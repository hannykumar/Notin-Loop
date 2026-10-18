from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.error import URLError
from urllib.request import Request, urlopen

from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from agent.incident_catalog import all_incident_definitions, catalog_keywords
from agent.model_runtime import passive_model_status
from agent.service_memory import load_service_memory
from runtime.resilience import run_with_retry
from runtime.settings import get_settings

RUNBOOKS_DIR = Path("runbooks")
METADATA_RE = re.compile(r"^-\s*([^:]+):\s*(.+?)\s*$")


def _infer_incident_type_from_filename(name: str) -> str:
    lowered = name.lower()
    if "database" in lowered or "db_" in lowered or "db-" in lowered:
        return "DatabaseConnectionFailure"
    if "tls" in lowered or "certificate" in lowered or "x509" in lowered:
        return "TLSCertificateExpiry"
    if "node_not_ready" in lowered or "node" in lowered:
        return "NodeNotReady"
    if "disk" in lowered or "storage" in lowered:
        return "DiskPressure"
    if "queue" in lowered or "worker_lag" in lowered or "backlog" in lowered:
        return "QueueBacklog"
    if "dependency" in lowered or "latency" in lowered:
        return "DependencyLatency"
    if "deploy" in lowered or "regression" in lowered or "release" in lowered:
        return "DeploymentRegression"
    if "dns" in lowered:
        return "DNSFailure"
    if "503" in lowered or "upstream" in lowered:
        return "Service503"
    if "crashloop" in lowered or "oom" in lowered:
        return "CrashLoopBackOff"
    if "cpu" in lowered:
        return "HighCPU"
    if "memory" in lowered:
        return "HighMemory"
    return "Unknown"


def lexical_terms(query: str) -> List[str]:
    extra_stopwords = {"pod", "pods", "service", "services", "host", "hosts", "errors", "error", "failed", "failing"}
    return [
        token
        for token in re.split(r"\W+", query.lower())
        if len(token) > 2 and token not in ENGLISH_STOP_WORDS and token not in extra_stopwords
    ]


def _split_metadata_values(value: str) -> List[str]:
    return [item.strip().lower() for item in re.split(r"[;,]", value) if item.strip()]


def _parse_runbook_metadata(text: str) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    in_metadata = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_metadata = line[3:].strip().lower() == "metadata"
            continue
        if not in_metadata:
            continue
        match = METADATA_RE.match(line.strip())
        if not match:
            continue
        key = match.group(1).strip().lower().replace(" ", "_")
        value = match.group(2).strip()
        if key in {"services", "common_root_causes", "signals", "owners", "components"}:
            metadata[key] = _split_metadata_values(value)
        else:
            metadata[key] = value
    return metadata


@lru_cache(maxsize=1)
def _runbook_chunks() -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []
    active_incident_types = {definition.incident_type for definition in all_incident_definitions()}
    for path in sorted(RUNBOOKS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        metadata = _parse_runbook_metadata(text)
        incident_type = str(metadata.get("incident_type") or _infer_incident_type_from_filename(path.name))
        if active_incident_types and incident_type not in active_incident_types:
            continue
        current_section = "Overview"
        current_lines: List[str] = []
        for line in text.splitlines():
            if line.startswith("## "):
                if current_lines:
                    chunks.append(
                        {
                            "source_file": path.name,
                            "incident_type": incident_type,
                            "section": current_section,
                            "text": "\n".join(current_lines).strip(),
                            "metadata": metadata,
                        }
                    )
                current_section = line[3:].strip()
                current_lines = [line]
            else:
                current_lines.append(line)
        if current_lines:
            chunks.append(
                {
                    "source_file": path.name,
                    "incident_type": incident_type,
                    "section": current_section,
                    "text": "\n".join(current_lines).strip(),
                    "metadata": metadata,
                }
            )
    return chunks


@lru_cache(maxsize=1)
def _tfidf_bundle() -> Tuple[TfidfVectorizer, Any]:
    chunks = _runbook_chunks()
    corpus = [f"{item['incident_type']} {item['source_file']} {item['section']}\n{item['text']}" for item in chunks]
    vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=1)
    matrix = vectorizer.fit_transform(corpus)
    return vectorizer, matrix


def _lexical_score(text: str, terms: List[str], keywords: List[str], query_text: str) -> float:
    lowered = text.lower()
    score = 0.0
    for token in terms:
        score += float(lowered.count(token))
    for keyword in keywords:
        if keyword and keyword in lowered and keyword in query_text:
            score += 2.5
    return score


def _vector_scores(query: str) -> List[float]:
    if not query.strip():
        return [0.0 for _ in _runbook_chunks()]
    vectorizer, matrix = _tfidf_bundle()
    query_vec = vectorizer.transform([query])
    sims = cosine_similarity(query_vec, matrix)[0]
    return [float(value) for value in sims]


def _candidate_prior(incident_type: str, incident: Dict[str, Any] | None) -> float:
    if incident is None:
        return 0.0
    text = " ".join(
        [
            str(incident.get("title", "")),
            str(incident.get("description", "")),
            str(incident.get("service", "")),
        ]
    ).lower()
    matches = sum(1 for keyword in catalog_keywords(incident_type) if keyword in text)
    return min(matches * 0.15, 0.6)


def _incident_context(incident: Dict[str, Any] | None) -> Dict[str, str]:
    if incident is None:
        return {"service": "", "severity": "", "component": "", "namespace": ""}
    trigger = dict(incident.get("trigger", {}) or {})
    labels = dict(trigger.get("labels", {}) or {})
    return {
        "service": str(incident.get("service") or labels.get("service") or "").strip().lower(),
        "severity": str(incident.get("severity") or labels.get("severity") or "").strip().lower(),
        "component": str(incident.get("component") or labels.get("component") or incident.get("service") or "").strip().lower(),
        "namespace": str(incident.get("namespace") or labels.get("namespace") or "").strip().lower(),
    }


def _metadata_score(chunk: Dict[str, Any], incident: Dict[str, Any] | None, query_terms: List[str]) -> float:
    metadata = dict(chunk.get("metadata", {}) or {})
    incident_meta = _incident_context(incident)
    score = 0.0
    services = [str(item).lower() for item in metadata.get("services", [])] if isinstance(metadata.get("services"), list) else []
    severity = str(metadata.get("severity", "")).strip().lower()
    primary_signal = str(metadata.get("primary_signal", "")).strip().lower()

    if services:
        if "any" in services:
            score += 0.08
        if incident_meta["service"] and incident_meta["service"] in services:
            score += 0.18
        if incident_meta["component"] and incident_meta["component"] in services:
            score += 0.12
    if severity and incident_meta["severity"] and severity == incident_meta["severity"]:
        score += 0.08
    if primary_signal:
        score += 0.04 if any(token in primary_signal for token in query_terms[:6]) else 0.0
    if incident_meta["service"] and incident_meta["service"] in str(chunk.get("text", "")).lower():
        score += 0.05
    return score


def _passes_metadata_prefilter(chunk: Dict[str, Any], incident: Dict[str, Any] | None) -> bool:
    incident_meta = _incident_context(incident)
    if not any(incident_meta.values()):
        return True
    metadata = dict(chunk.get("metadata", {}) or {})
    services = [str(item).lower() for item in metadata.get("services", [])] if isinstance(metadata.get("services"), list) else []
    severity = str(metadata.get("severity", "")).strip().lower()

    if incident_meta["service"] and services and "any" not in services and incident_meta["service"] not in services and incident_meta["component"] not in services:
        return False
    if incident_meta["severity"] and severity and incident_meta["severity"] != severity:
        return False
    return True


def _service_memory_prior(incident_type: str, incident: Dict[str, Any] | None) -> float:
    incident_meta = _incident_context(incident)
    service = incident_meta["service"]
    if not service:
        return 0.0
    memory = load_service_memory(service, namespace=incident_meta["namespace"] or "prod")
    payload = dict(memory.get("payload") or {})
    historical = [str(item.get("type", "")).strip() for item in payload.get("incident_history", []) if isinstance(item, dict)]
    feedback_matches = sum(
        1
        for item in payload.get("validated_feedback_priors", [])
        if isinstance(item, dict) and str(item.get("incident_type", "")).strip() == incident_type
    )
    # Human feedback is a weak prior only; live evidence and retrieval scores must dominate it.
    return (0.15 if incident_type in historical else 0.0) + min(feedback_matches * 0.04, 0.12)


def _hint_text(query: str, incident: Dict[str, Any] | None, evidence: Dict[str, Any] | None) -> str:
    parts = [str(query or "")]
    if incident:
        parts.extend(
            [
                str(incident.get("title", "")),
                str(incident.get("description", "")),
                str(incident.get("service", "")),
                str(incident.get("component", "")),
            ]
        )
        incident_meta = _incident_context(incident)
        if incident_meta["service"]:
            memory = load_service_memory(incident_meta["service"], namespace=incident_meta["namespace"] or "prod")
            payload = dict(memory.get("payload") or {})
            for item in payload.get("incident_history", [])[:4]:
                if isinstance(item, dict):
                    parts.append(str(item.get("type", "")))
                    parts.append(str(item.get("summary", "")))
    if evidence:
        parts.extend(str(line) for line in list(evidence.get("logs_tail") or [])[-5:])
        parts.extend(str(line) for line in list(evidence.get("cluster_events") or [])[-4:])
        parts.append(" ".join(str(value) for value in dict(evidence.get("metrics") or {}).values()))
        parts.append(" ".join(str(value) for value in dict(evidence.get("prometheus") or {}).values()))
    return " ".join(part for part in parts if str(part).strip()).lower()


def _incident_hint_scores(query: str, incident: Dict[str, Any] | None, evidence: Dict[str, Any] | None) -> Dict[str, float]:
    text = _hint_text(query, incident, evidence)
    if not text:
        return {}
    scores: Dict[str, float] = {}
    for definition in all_incident_definitions():
        matches = sum(1 for keyword in definition.keywords if keyword and keyword in text)
        if matches:
            scores[definition.incident_type] = min(matches * 0.14, 0.7)
    return scores


def _section_priority(section: str) -> float:
    lowered = str(section or "").strip().lower()
    if lowered in {"symptoms", "diagnosis rules", "evidence to collect", "fast checks (2 minutes)"}:
        return 0.12
    if lowered in {"mitigation (safe)", "permanent fix"}:
        return 0.06
    return 0.02


def _diverse_select(chunks: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    candidates = list(chunks)
    selected: List[Dict[str, Any]] = []
    source_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    section_counts: Counter[str] = Counter()

    while candidates and len(selected) < limit:
        best_index = 0
        best_score = float("-inf")
        for idx, chunk in enumerate(candidates):
            score = float(chunk.get("score", 0.0))
            source_file = str(chunk.get("source_file", ""))
            incident_type = str(chunk.get("incident_type", "Unknown"))
            section = str(chunk.get("section", ""))
            score -= source_counts[source_file] * 0.3
            score -= max(type_counts[incident_type] - 1, 0) * 0.18
            score -= max(section_counts[section] - 1, 0) * 0.08
            if source_counts[source_file] == 0:
                score += 0.05
            if type_counts[incident_type] == 0:
                score += 0.04
            if score > best_score:
                best_score = score
                best_index = idx
        chosen = candidates.pop(best_index)
        selected.append(chosen)
        source_counts[str(chosen.get("source_file", ""))] += 1
        type_counts[str(chosen.get("incident_type", "Unknown"))] += 1
        section_counts[str(chosen.get("section", ""))] += 1
    return selected


def _rerank_chunk(chunk: Dict[str, Any], incident: Dict[str, Any] | None, evidence: Dict[str, Any] | None) -> float:
    score = 0.0
    incident_type = str(chunk.get("incident_type", "Unknown"))
    query_terms = lexical_terms(" ".join([str((incident or {}).get("title", "")), str((incident or {}).get("description", ""))]))
    score += _metadata_score(chunk, incident, query_terms)
    score += _section_priority(str(chunk.get("section", "")))
    if incident is not None:
        text = f"{incident.get('title', '')} {incident.get('description', '')}".lower()
        score += sum(0.05 for keyword in catalog_keywords(incident_type) if keyword in text)
    if evidence:
        logs_text = " ".join(evidence.get("logs_tail", [])).lower()
        event_text = " ".join(evidence.get("cluster_events", [])).lower()
        prom_text = " ".join(str(value) for value in (evidence.get("prometheus") or {}).values()).lower()
        loki_text = " ".join((evidence.get("loki") or {}).get("lines", [])).lower()
        tempo_text = " ".join(str(item) for item in (evidence.get("tempo") or {}).get("spans", [])).lower()
        deploy_text = " ".join(str(item) for item in evidence.get("recent_deploys", [])).lower()
        score += sum(
            0.05
            for keyword in catalog_keywords(incident_type)
            if keyword in logs_text or keyword in event_text or keyword in prom_text or keyword in loki_text or keyword in tempo_text or keyword in deploy_text
        )
    return score


def _quality_score(chunks: List[Dict[str, Any]], query_terms: List[str]) -> Dict[str, Any]:
    if not chunks:
        return {
            "overall": 0.0,
            "top_score": 0.0,
            "margin": 0.0,
            "type_consensus": 0.0,
            "query_coverage": 0.0,
            "metadata_alignment": 0.0,
            "needs_retry": True,
        }

    top_score = float(chunks[0].get("score", 0.0))
    second_score = float(chunks[1].get("score", 0.0)) if len(chunks) > 1 else 0.0
    margin = max(top_score - second_score, 0.0)
    top_types = [str(chunk.get("incident_type", "Unknown")) for chunk in chunks[:3]]
    type_counts = Counter(top_types)
    type_consensus = max(type_counts.values()) / max(len(top_types), 1)
    top_text = str(chunks[0].get("text", "")).lower()
    if query_terms:
        query_coverage = sum(1 for token in query_terms if token in top_text) / len(query_terms)
    else:
        query_coverage = 0.0
    metadata_alignment = sum(float(chunk.get("score_breakdown", {}).get("metadata", 0.0)) for chunk in chunks[:3]) / max(len(chunks[:3]), 1)
    source_diversity = len({str(chunk.get("source_file", "")) for chunk in chunks[:5]}) / max(len(chunks[:5]), 1)

    normalized_top = min(top_score / 8.0, 1.0)
    normalized_margin = min(margin / 3.0, 1.0)
    normalized_metadata = min(metadata_alignment / 0.25, 1.0)
    overall = round((normalized_top + normalized_margin + type_consensus + query_coverage + normalized_metadata + source_diversity) / 6.0, 3)
    return {
        "overall": overall,
        "top_score": round(top_score, 3),
        "margin": round(margin, 3),
        "type_consensus": round(type_consensus, 3),
        "query_coverage": round(query_coverage, 3),
        "metadata_alignment": round(metadata_alignment, 3),
        "source_diversity": round(source_diversity, 3),
        "needs_retry": overall < 0.55,
    }


def _retrieval_guardrail_triggered(quality: Dict[str, Any], candidates: List[Dict[str, Any]]) -> bool:
    if not candidates:
        return True
    if float(quality.get("overall", 0.0)) < 0.6:
        return True
    if float(quality.get("type_consensus", 0.0)) < 0.6:
        return True
    if len(candidates) > 1 and float(quality.get("margin", 0.0)) < 0.35 and float(quality.get("type_consensus", 0.0)) < 0.75:
        return True
    return False


def _retrieval_explanation(
    query: str,
    selected: List[Dict[str, Any]],
    quality: Dict[str, Any],
    incident: Dict[str, Any] | None,
    *,
    prefilter_used: bool = False,
    prefilter_matches: int = 0,
) -> Dict[str, Any]:
    incident_meta = _incident_context(incident)
    service_memory = load_service_memory(incident_meta["service"], namespace=incident_meta["namespace"] or "prod") if incident_meta["service"] else {}
    memory_payload = dict(service_memory.get("payload") or {})
    return {
        "query": query,
        "filters": {key: value for key, value in incident_meta.items() if value},
        "service_memory": {
            "service": service_memory.get("service", ""),
            "recent_incident_types": [item.get("type", "") for item in memory_payload.get("incident_history", [])[:3] if isinstance(item, dict)],
            "dashboards": [item.get("uid", "") for item in memory_payload.get("dashboards", [])[:3] if isinstance(item, dict)],
        },
        "top_sources": [
            {
                "source_file": chunk.get("source_file", ""),
                "section": chunk.get("section", ""),
                "incident_type": chunk.get("incident_type", ""),
                "score": float(chunk.get("score", 0.0)),
                "score_breakdown": chunk.get("score_breakdown", {}),
            }
            for chunk in selected[:5]
        ],
        "selection_strategy": "lexical + tfidf + evidence context + diversity",
        "metadata_prefilter": {
            "used": prefilter_used,
            "match_count": prefilter_matches,
        },
        "quality": quality,
    }


def _candidate_rankings(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    totals: Dict[str, float] = defaultdict(float)
    supporting_chunks: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for chunk in chunks:
        incident_type = str(chunk.get("incident_type", "Unknown"))
        totals[incident_type] += float(chunk.get("score", 0.0))
        supporting_chunks[incident_type].append(chunk)

    grand_total = sum(totals.values()) or 1.0
    rankings = []
    for incident_type, score in sorted(totals.items(), key=lambda item: item[1], reverse=True):
        rankings.append(
            {
                "incident_type": incident_type,
                "retrieval_score": round(score, 3),
                "retrieval_confidence": round(score / grand_total, 3),
                "supporting_chunks": [
                    {
                        "source_file": chunk.get("source_file"),
                        "section": chunk.get("section"),
                        "score": round(float(chunk.get("score", 0.0)), 3),
                    }
                    for chunk in supporting_chunks[incident_type][:3]
                ],
            }
        )
    return rankings


def retrieve_runbook_context(
    query: str,
    *,
    incident: Dict[str, Any] | None = None,
    evidence: Dict[str, Any] | None = None,
    limit: int = 8,
) -> Dict[str, Any]:
    terms = lexical_terms(query)
    query_text = query.lower()
    vector_scores = _vector_scores(query)
    hint_scores = _incident_hint_scores(query, incident, evidence)
    chunks: List[Dict[str, Any]] = []

    for idx, base in enumerate(_runbook_chunks()):
        chunk = dict(base)
        incident_type = str(chunk.get("incident_type", "Unknown"))
        keywords = catalog_keywords(incident_type)
        lexical_score = _lexical_score(chunk["text"], terms, keywords, query_text)
        vector_score = vector_scores[idx] if idx < len(vector_scores) else 0.0
        prior_score = _candidate_prior(incident_type, incident)
        service_memory_score = _service_memory_prior(incident_type, incident)
        metadata_score = _metadata_score(chunk, incident, terms)
        rerank_score = _rerank_chunk(chunk, incident, evidence)
        hint_score = hint_scores.get(incident_type, 0.0)
        combined = lexical_score + (vector_score * 6.0) + prior_score + service_memory_score + rerank_score + hint_score
        if combined <= 0:
            continue
        chunk.update(
            {
                "score": round(combined, 3),
                "score_breakdown": {
                    "lexical": round(lexical_score, 3),
                    "vector": round(vector_score, 3),
                    "prior": round(prior_score, 3),
                    "service_memory": round(service_memory_score, 3),
                    "metadata": round(metadata_score, 3),
                    "rerank": round(rerank_score, 3),
                    "hint": round(hint_score, 3),
                },
                "text": str(chunk.get("text", ""))[:2400],
            }
        )
        chunks.append(chunk)

    chunks.sort(key=lambda item: (float(item.get("score", 0.0)), item.get("source_file", "")), reverse=True)
    prefiltered = [chunk for chunk in chunks if _passes_metadata_prefilter(chunk, incident)]
    preselected = (prefiltered or chunks)[: max(limit * 4, 16)]
    preselected.sort(key=lambda item: (float(item.get("score", 0.0)), item.get("source_file", "")), reverse=True)
    selected = _diverse_select(preselected, limit)
    quality = _quality_score(selected, terms)
    candidates = _candidate_rankings(selected)
    explanation = _retrieval_explanation(
        query,
        selected,
        quality,
        incident,
        prefilter_used=bool(prefiltered),
        prefilter_matches=len(prefiltered),
    )
    predicted_type = candidates[0]["incident_type"] if candidates else "Unknown"
    if len(candidates) > 1 and math.isclose(
        float(candidates[0].get("retrieval_score", 0.0)),
        float(candidates[1].get("retrieval_score", 0.0)),
        rel_tol=0.02,
        abs_tol=0.25,
    ):
        predicted_type = "Unknown"
    if _retrieval_guardrail_triggered(quality, candidates):
        predicted_type = "Unknown"
    explanation["guardrail"] = {
        "predicted_type_allowed": predicted_type != "Unknown",
        "reason": "retrieval_quality_gate" if predicted_type == "Unknown" else "passed",
    }

    return {
        "retrieved": selected,
        "top_chunks": selected[:5],
        "predicted_type": predicted_type,
        "retrieval_quality": quality,
        "retrieval_explanation": explanation,
        "candidate_diagnoses": candidates,
        "query": query,
    }


def retrieve_runbook_chunks(
    query: str,
    limit: int = 8,
    *,
    incident: Dict[str, Any] | None = None,
    evidence: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    return retrieve_runbook_context(query, incident=incident, evidence=evidence, limit=limit)["retrieved"]


def summarize_retrieval_context(
    query: str,
    retrieved: List[Dict[str, Any]],
    *,
    incident: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    quality = _quality_score(retrieved, lexical_terms(query))
    candidates = _candidate_rankings(retrieved)
    explanation = _retrieval_explanation(query, retrieved, quality, incident)
    predicted_type = candidates[0]["incident_type"] if candidates else "Unknown"
    if len(candidates) > 1 and abs(float(candidates[0].get("retrieval_score", 0.0)) - float(candidates[1].get("retrieval_score", 0.0))) <= 0.25:
        predicted_type = "Unknown"
    if _retrieval_guardrail_triggered(quality, candidates):
        predicted_type = "Unknown"
    explanation["guardrail"] = {
        "predicted_type_allowed": predicted_type != "Unknown",
        "reason": "retrieval_quality_gate" if predicted_type == "Unknown" else "passed",
    }
    return {
        "retrieved": retrieved,
        "top_chunks": retrieved[:5],
        "predicted_type": predicted_type,
        "retrieval_quality": quality,
        "retrieval_explanation": explanation,
        "candidate_diagnoses": candidates,
        "query": query,
    }


def _retry_query_prompt(incident: Dict[str, Any], evidence: Dict[str, Any], candidate_diagnoses: List[Dict[str, Any]]) -> str:
    top_candidates = [
        {
            "incident_type": item.get("incident_type", "Unknown"),
            "score": item.get("score", item.get("retrieval_confidence", 0.0)),
        }
        for item in candidate_diagnoses[:3]
    ]
    payload = {
        "incident": {
            "incident_id": incident.get("incident_id"),
            "service": incident.get("service"),
            "namespace": incident.get("namespace"),
            "description": incident.get("description"),
        },
        "evidence": {
            "metrics": evidence.get("metrics", {}),
            "logs_tail": list(evidence.get("logs_tail") or [])[-4:],
            "cluster_events": list(evidence.get("cluster_events") or [])[-3:],
        },
        "top_candidates": top_candidates,
    }
    return (
        "Rewrite the incident search query for retrieval. "
        "Return only a JSON object like {\"query\": \"...\"}. "
        "Keep it short, concrete, and focused on the most discriminating symptoms.\n"
        f"{json.dumps(payload, indent=2)}"
    )


def _rewrite_query_with_ollama(prompt: str) -> str:
    settings = get_settings()
    req = Request(
        url=f"{settings.planner_base_url.rstrip('/')}/api/chat",
        headers={"Content-Type": "application/json"},
        data=json.dumps(
            {
                "model": settings.planner_model,
                "stream": False,
                "think": False,
                "messages": [
                    {
                        "role": "system",
                        "content": "You rewrite retrieval queries for an SRE incident assistant. Output JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "format": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                "options": {"temperature": 0.1, "num_ctx": settings.planner_context_tokens, "num_predict": 128},
            }
        ).encode("utf-8"),
        method="POST",
    )
    with urlopen(req, timeout=min(get_settings().planner_timeout_seconds, 8.0)) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    content = str(((raw.get("message") or {}).get("content") or "")).strip()
    return str(json.loads(content).get("query", "")).strip()


def _rewrite_query_with_openai(prompt: str) -> str:
    settings = get_settings()
    headers = {"Content-Type": "application/json"}
    if settings.planner_api_key:
        headers["Authorization"] = f"Bearer {settings.planner_api_key}"
    req = Request(
        url=f"{settings.planner_base_url.rstrip('/')}/v1/chat/completions",
        headers=headers,
        data=json.dumps(
            {
                "model": settings.planner_model,
                "temperature": 0.1,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "retrieval_query",
                        "schema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    },
                },
                "messages": [
                    {
                        "role": "system",
                        "content": "You rewrite retrieval queries for an SRE incident assistant. Output JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
            }
        ).encode("utf-8"),
        method="POST",
    )
    with urlopen(req, timeout=min(get_settings().planner_timeout_seconds, 8.0)) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    choices = list(raw.get("choices") or [])
    if not choices:
        return ""
    content = str(((choices[0] or {}).get("message") or {}).get("content") or "").strip()
    return str(json.loads(content).get("query", "")).strip()


def _maybe_ai_retry_query(incident: Dict[str, Any], evidence: Dict[str, Any], candidate_diagnoses: List[Dict[str, Any]]) -> str:
    settings = get_settings()
    if settings.planner_provider == "deterministic":
        return ""
    if passive_model_status().get("cooldown_active"):
        return ""
    prompt = _retry_query_prompt(incident, evidence, candidate_diagnoses)
    try:
        caller = _rewrite_query_with_openai if settings.planner_provider in {"openai", "openai_compatible"} else _rewrite_query_with_ollama
        query = run_with_retry(
            lambda: caller(prompt),
            retries=0,
            timeout_seconds=min(settings.planner_timeout_seconds, 8.0),
            operation_name="retrieval:query_rewrite",
            retry_exceptions=(URLError, TimeoutError, ValueError, json.JSONDecodeError),
        )
        return str(query or "").strip()
    except Exception:
        return ""


def build_retry_query(incident: Dict[str, Any], evidence: Dict[str, Any], candidate_diagnoses: List[Dict[str, Any]]) -> str:
    pieces = [str(incident.get("description", ""))]
    top_candidate = candidate_diagnoses[0]["incident_type"] if candidate_diagnoses else "Unknown"
    if top_candidate != "Unknown":
        pieces.append(f"likely incident type {top_candidate}")
    metrics = evidence.get("metrics", {})
    for key in ["error_rate_percent", "dns_error_rate_percent", "memory_percent", "p95_latency_ms", "cpu_percent"]:
        if key in metrics:
            pieces.append(f"{key} {metrics[key]}")
    logs = evidence.get("logs_tail", []) or []
    if logs:
        pieces.append(" ".join(str(line) for line in logs[-3:]))
    events = evidence.get("cluster_events", []) or []
    if events:
        pieces.append(" ".join(str(line) for line in events[-2:]))
    deterministic_query = " ".join(part for part in pieces if str(part).strip())
    ai_query = _maybe_ai_retry_query(incident, evidence, candidate_diagnoses)
    return ai_query or deterministic_query
