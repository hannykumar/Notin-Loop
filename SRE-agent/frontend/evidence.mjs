export const escape = (value) => String(value ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"})[c]);
export const readable = value => String(value ?? "").replaceAll("_", " ");

function text(value) {
  if (value == null) return "Not observed";
  if (Array.isArray(value)) return value.map(text).join("; ");
  if (typeof value === "object") return Object.entries(value).map(([key, item]) => `${readable(key)}: ${text(item)}`).join(" · ");
  return String(value);
}

export function observation(value) {
  if (Array.isArray(value)) return `<ul>${value.slice(0, 12).map(item => `<li>${escape(text(item))}</li>`).join("")}</ul>`;
  if (value && typeof value === "object") return `<dl class="observation">${Object.entries(value).slice(0, 16).map(([key, item]) => `<dt>${escape(readable(key))}</dt><dd>${escape(text(item))}</dd>`).join("")}</dl>`;
  return `<p>${escape(text(value))}</p>`;
}

export function timeline(activity = []) {
  return `<section class="panel"><h2>Investigation timeline</h2>${activity.map(item => `<div class="evidence"><small>${escape(readable(item.stage))} · ${escape(item.status)}</small><p>${escape(item.summary)}</p></div>`).join("") || '<p class="muted">Activity will appear when the investigation completes.</p>'}</section>`;
}

export function recovery(result) {
  if (result.execution_mode === "preview") return "";
  const checks = result.verification?.checks || [];
  const samples = result.verification?.stabilization?.samples || [];
  return `<section class="panel"><h2>Recovery measurements</h2>
    ${result.execution_mode === "simulate" ? '<p class="notice">Fixture simulation. These measurements describe the simulated incident, not a real cluster.</p>' : ''}
    <div class="table-wrap"><table><thead><tr><th>CHECK</th><th>BEFORE</th><th>AFTER</th><th>RESULT</th></tr></thead><tbody>
    ${checks.map(check => `<tr><td>${escape(check.type === 'log_absent' ? `Logs exclude: ${(check.tokens || []).join(', ')}` : readable(check.metric || check.type))}</td><td>${escape(check.before ?? '—')}</td><td>${escape(check.after ?? (check.type === 'log_absent' ? (check.passed ? 'Absent' : 'Found') : '—'))}</td><td class="${check.passed ? 'good' : 'warn'}">${check.passed ? 'Passed' : 'Not met'}${check.optional ? ' · optional' : ''}</td></tr>`).join('')}
    </tbody></table></div>${!checks.length ? `<p class="muted">${escape(result.verification?.reason || 'No recovery checks were recorded.')}</p>` : ''}
    ${samples.length ? `<h3>Stabilization window · ${samples.length} samples</h3><div class="table-wrap"><table><thead><tr><th>OBSERVED</th><th>ERROR RATE (%)</th><th>LATENCY (ms)</th></tr></thead><tbody>${samples.map(sample => `<tr><td>${escape(new Date(sample.observed_at * 1000).toLocaleTimeString())}</td><td>${escape(sample.evidence?.metrics?.error_rate_percent ?? '—')}</td><td>${escape(sample.evidence?.metrics?.p95_latency_ms ?? '—')}</td></tr>`).join('')}</tbody></table></div>` : ''}
    </section>`;
}

export function hypotheses(items = [], ledger = []) {
  const ids = new Set(ledger.map(item => item.evidence_id));
  return items.map(h => `<div class="evidence"><h3>${escape(h.cause || h.incident_type || h.hypothesis)}</h3>
    <div class="progress" role="meter" aria-label="Hypothesis confidence" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(Number(h.confidence || 0) * 100)}"><i style="width:${Math.max(0, Math.min(100, Number(h.confidence || 0) * 100))}%"></i></div>
    <small>${Math.round(Number(h.confidence || 0) * 100)}% confidence</small>
    <p>${(h.supporting_evidence_ids || []).map(id => ids.has(id) ? `<a href="#evidence-${escape(id)}" data-evidence="${escape(id)}">${escape(id)}</a>` : `<span>${escape(id)}</span>`).join(' · ') || 'No supporting citations yet.'}</p>
    ${(h.rationale || []).map(line => `<p>${escape(line)}</p>`).join('')}
    ${(h.contradicting_evidence_ids || []).map(id => `<p class="warn">Contradicting evidence: ${escape(id)}</p>`).join('')}
    ${(h.evidence_needed || h.missing_evidence || []).map(line => `<p class="muted">Still needed: ${escape(line)}</p>`).join('')}
    </div>`).join('');
}

export function evaluationCases(profiles = {}) {
  return Object.entries(profiles).map(([name, profile]) => `<details><summary>${escape(readable(name))} · case results</summary><div class="table-wrap"><table><thead><tr><th>CASE</th><th>EXPECTED</th><th>DIAGNOSED</th><th>MODEL / FALLBACK</th><th>RESULT</th></tr></thead><tbody>${(profile.rows || []).map(row => `<tr><td>${escape(row.incident)}</td><td>${escape(row.expected)}</td><td>${escape(row.predicted)}</td><td>${escape(readable(row.planner_backend))}${row.planner_error ? `<p class="warn">${escape(row.planner_error)}</p>` : ''}</td><td class="${row.type_match ? 'good' : 'warn'}">${row.type_match ? 'Matched' : 'Mismatch'}</td></tr>`).join('')}</tbody></table></div>${!profile.rows?.length ? '<p class="muted">No cases completed for this profile.</p>' : ''}</details>`).join('');
}
