import {escape, readable, observation, recovery, hypotheses, timeline, evaluationCases} from "/assets/evidence.mjs";

const main = document.querySelector("#main");
const state = {
  runs: [],
  scenarios: [],
  health: {},
  run: null,
  step: 0,
  busy: false,
  evaluationStatus: "idle",
};
const percent = (value) =>
  value == null ? "—" : `${Math.round(Number(value) * 100)}%`;
const badge = (value) =>
  `<span class="badge">${escape(readable(value || "unknown"))}</span>`;
const json = (title, value) =>
  `<details><summary>${escape(title)}</summary><pre>${escape(JSON.stringify(value, null, 2))}</pre></details>`;
const card = (label, value, detail = "") =>
  `<div class="card"><div class="label">${escape(label)}</div><div class="value">${escape(value)}</div><small>${escape(detail)}</small></div>`;
const title = (heading, subtitle, action = "") =>
  `<div class="title-row"><div><h1>${escape(heading)}</h1><p class="muted">${escape(subtitle)}</p></div>${action}</div>`;
function showError(error) {
  const box = document.querySelector("#error");
  box.textContent = error.message || String(error);
  box.hidden = false;
}
async function api(path, body) {
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      "X-API-Token": sessionStorage.getItem("sre-token") || "",
    },
    ...(body === undefined
      ? {}
      : { method: "POST", body: JSON.stringify(body) }),
  });
  const payload = await response.json();
  if (!response.ok)
    throw new Error(
      typeof payload.detail === "string"
        ? payload.detail
        : JSON.stringify(payload.detail || payload),
    );
  return payload;
}
function page() {
  return location.hash.slice(1).split("/")[0] || "incidents";
}
function render() {
  const current = page();
  document
    .querySelectorAll("[data-page]")
    .forEach((a) => a.classList.toggle("active", a.dataset.page === current));
  document.querySelector("#breadcrumb").textContent =
    `Operations / ${readable(current)}`;
  if (current === "settings") return renderSettings();
  if (current === "evaluation") return renderEvaluation();
  if (location.hash.startsWith("#incidents/") && state.run) return renderRun();
  renderIncidents();
}
function renderIncidents() {
  const runs = state.runs;
  main.innerHTML =
    title(
      "Incident workbench",
      "Investigate the cause. Review the evidence. Approve the next action.",
      '<button data-refresh>Refresh incidents</button>',
    ) +
    `<div class="cards">${card("Investigations", runs.length, "Most recent 50 runs")}${card("Awaiting approval", runs.filter((r) => r.status === "awaiting_approval").length)}${card("Executing", runs.filter((r) => ["executing", "execution_queued", "verifying"].includes(r.status)).length)}${card("Planner", readable(state.health.runtime_profile?.planner_provider || "unknown"), state.health.runtime_profile?.planner_model || "")}</div>` +
    `<section class="panel"><h2>Start an investigation</h2><p class="muted">Replay a controlled incident, or open an incident received from Grafana below.</p><form id="start-form" class="toolbar"><label for="scenario">Scenario</label><select id="scenario" name="incident_id">${state.scenarios.map((s) => `<option value="${escape(s.incident_id)}" ${s.incident_id === "INC-002" ? "selected" : ""}>${escape(s.title)}</option>`).join("")}</select><button class="primary" ${state.busy || !state.scenarios.length ? "disabled" : ""}>${state.busy ? "Starting…" : "Start investigation →"}</button></form><small>Source: ${escape(state.health.mcp_backend || "unknown")}. Proposals require explicit approval.</small></section>` +
    `<section class="panel"><h2>Recent incidents</h2>${runs.length ? `<div class="table-wrap"><table><thead><tr><th>INCIDENT</th><th>SERVICE / SOURCE</th><th>STATUS</th><th>CREATED</th></tr></thead><tbody>${runs.map((r) => `<tr><td><button data-run="${escape(r.run_id)}">${escape(r.request?.incident_id || r.run_id)}</button><br><small>${escape(r.plan_result?.diagnosis || "Investigation pending")}</small></td><td>${escape(r.plan_result?.incident?.service || r.request?.incident_context?.service || "—")}<br><small>${escape(readable(r.request?.source || "ui"))}</small></td><td>${badge(r.status)}</td><td>${escape(new Date(r.created_at).toLocaleString())}</td></tr>`).join("")}</tbody></table></div>` : '<p class="empty">No investigations yet. Start with the dependency 503 scenario.</p>'}</section>`;
}
function renderRun() {
  const run = state.run,
    result = run.plan_result || {},
    executed = run.execute_result,
    artifact = run.approval?.artifact || {};
  const pending = [
    "queued",
    "planning",
    "execution_queued",
    "executing",
    "verifying",
  ].includes(run.status);
  const steps = ["Incident", "Investigation", "Approval", "Verification"];
  main.innerHTML =
    title(
      result.incident?.title || run.request?.incident_id || run.run_id,
      `${run.run_id} · ${result.incident?.service || "Incident context"} · ${readable(run.status)}`,
      "<button data-back>← All incidents</button>",
    ) +
    `<div class="toolbar">${badge(run.status)}${badge(result.planner_backend || state.health.runtime_profile?.planner_provider)}${pending ? '<span class="muted"><span class="spinner"></span>Working · updates automatically</span>' : ""}</div>` +
    (result.planner_error
      ? `<div class="notice">One or more model steps failed. Deterministic fallback was used for those steps.<details><summary>Model error</summary>${escape(result.planner_error)}</details></div>`
      : "") +
    `<div class="steps" role="group" aria-label="Incident workflow">${steps.map((s, i) => `<button aria-pressed="${state.step === i}" data-step="${i}" class="${state.step === i ? "active" : ""}" ${i > 0 && !run.plan_result ? "disabled" : ""}><span>0${i + 1}</span>${s}</button>`).join("")}</div><div id="stage"></div>`;
  const stage = main.querySelector("#stage");
  if (state.step === 0)
    stage.innerHTML = `<div class="columns"><section class="panel"><h2>Incident context</h2><p>${escape(result.incident?.description || run.request?.incident_context?.summary || "Gathering incident context and operational evidence…")}</p><dl><dt>Service</dt><dd>${escape(result.incident?.service || run.request?.incident_context?.service || "—")}</dd><dt>Namespace</dt><dd>${escape(result.incident?.namespace || "—")}</dd><dt>Source</dt><dd>${escape(readable(run.request?.source))}</dd><dt>Execution mode</dt><dd>${escape(run.request?.execution_mode)}</dd></dl>${run.plan_result ? '<button class="primary" data-step="1">Review investigation →</button>' : ""}</section><section class="panel"><h2>What happens next</h2><div class="evidence"><h3>Read operational evidence</h3><p class="muted">Metrics, logs, workload state and recent changes.</p></div><div class="evidence"><h3>Review competing causes</h3><p class="muted">Check citations and uncertainty before acting.</p></div><div class="evidence"><h3>Approve one exact proposal</h3><p class="muted">Execution and recovery are recorded separately.</p></div></section></div>${json("Original incident request", run.request)}`;
  if (state.step === 1) {
    stage.innerHTML = `<div class="cards">${card("Diagnosis", result.diagnosis || "Unknown")}${card("Heuristic confidence", percent(result.confidence))}${card("Structured claim grounding", percent(result.claim_validation?.groundedness_score ?? result.claim_validation?.groundedness))}${card("Evidence sources", (result.evidence_ledger || []).length)}</div><div class="columns"><section class="panel"><h2>Investigation findings</h2><p>${escape(result.situation_summary || result.incident_brief?.summary)}</p>${hypotheses(result.hypotheses, result.evidence_ledger)}${json("Grounded report", result.incident_report)}<button class="primary" data-step="2">Review next action →</button></section><section class="panel"><h2>Evidence ledger</h2>${(result.evidence_ledger || []).map((e) => `<div class="evidence" id="evidence-${escape(e.evidence_id)}"><h3>${escape(readable(e.tool || e.source))} ${badge(e.status)}</h3><small>${escape(e.evidence_id)} · ${escape(e.observed_at || "")}</small>${observation(e.payload)}${json("Raw observation", e.payload)}</div>`).join("") || '<p class="muted">No evidence collected.</p>'}</section></div>${timeline(result.investigation_activity)}${json("Full reasoning trace", result.trace)}`;
  }
  if (state.step === 2) {
    const available =
      result.requires_human_approval &&
      run.approval?.status === "pending" &&
      run.status === "awaiting_approval";
    stage.innerHTML = `<div class="columns"><section class="panel"><h2>${result.requires_human_approval ? "Review the exact proposal" : "Human handoff"}</h2><p>${escape(result.approval_summary?.why_this_action || result.escalation_reason || result.handoff_summary?.summary)}</p>${result.requires_human_approval ? `<dl><dt>Action</dt><dd>${escape(readable(artifact.action?.action_type))}</dd><dt>Target</dt><dd>${escape(artifact.action?.target)} / ${escape(artifact.namespace)}</dd><dt>Risk</dt><dd>${escape(artifact.risk)}</dd><dt>Expires</dt><dd>${escape(artifact.expires_at)}</dd></dl><pre>${escape((result.planned_commands || []).join("\n"))}</pre><p class="muted">Proposal hash</p><p class="hash">${escape(artifact.proposal_hash)}</p><form id="approve-form"><label for="mode">Execution</label><select id="mode"><option value="preview">Preview only · no changes</option><option value="${state.health.mcp_backend === 'mock' ? 'simulate' : 'live'}">${state.health.mcp_backend === "mock" ? "Simulate in local fixtures" : "Execute in connected environment"}</option></select><label><input type="checkbox" id="approve-check" required> I reviewed this exact action, target, evidence and rollback.</label><button class="primary" ${!available || state.busy ? "disabled" : ""}>${state.busy ? "Submitting…" : "Approve this proposal"}</button></form>` : `<div class="notice">${escape(result.escalation_reason || "No executable proposal. Escalate for human investigation.")}</div>`}${executed ? '<button data-step="3">View verification →</button>' : ""}</section><section class="panel"><h2>Recovery and rollback</h2><p>${escape(artifact.expected_effect || "No approved recovery action.")}</p>${json("Verification plan", artifact.verification_plan || [])}<h3>Rollback</h3><pre>${escape((artifact.rollback_plan || []).join("\n") || "No rollback prepared")}</pre>${json("Remediation options and tradeoffs", result.remediation_options || [])}</section></div>`;
  }
  if (state.step === 3)
    stage.innerHTML = executed
      ? `<div class="cards">${card("Recovery outcome", readable(executed.verification_outcome || "not_run"))}${card("Execution", executed.status || "recorded")}${card("Samples", executed.verification?.stabilization?.sample_count ?? "—")}${card("Approval integrity", executed.executed_proposal_hash === artifact.proposal_hash ? "Matched" : "Check required")}</div><section class="panel"><h2>Recovery verification</h2><p>${escape(executed.improvement_summary || "Review the deterministic measurements below.")}</p>${executed.execution_mode === "preview" ? '<div class="notice">Preview only. No action was applied and recovery was not measured.</div>' : ""}${recovery(executed)}${json("Raw verification record", executed.verification)}${json("Evidence before", result.evidence)}${json("Evidence after", executed.evidence_after)}${json("Execution record", executed.execution_results)}</section>`
      : `<section class="panel"><p class="empty">${pending ? "Execution is in progress. This view updates automatically." : "No action has been executed for this incident."}</p></section>`;
  if (["failed", "execution_failed"].includes(run.status))
    stage.insertAdjacentHTML(
      "afterbegin",
      '<div class="notice">The job failed. Review the audit events for the recorded error.</div>',
    );
  stage.insertAdjacentHTML(
    "beforeend",
    '<details><summary>Audit trail</summary><button data-audit>Load audit events</button><div id="audit"></div></details>',
  );
}
function renderSettings() {
  main.innerHTML =
    title(
      "Runtime & access",
      "Check the model connection and authenticate to this workspace.",
    ) +
    `<div class="columns"><section class="panel"><h2>Model runtime</h2><dl><dt>Provider</dt><dd>${escape(state.health.runtime_profile?.planner_provider)}</dd><dt>Model</dt><dd>${escape(state.health.runtime_profile?.planner_model)}</dd><dt>Backend</dt><dd>${escape(state.health.mcp_backend)}</dd></dl><button data-probe>Check model & integrations</button><div id="probe"></div></section><section class="panel"><h2>API access</h2><p class="muted">The token stays in this browser tab's session. Use the role assigned by your runtime administrator.</p><form id="token-form"><label>API token<input type="password" name="token" autocomplete="off"></label><button class="primary">Connect</button></form></section></div>`;
}
async function renderEvaluation() {
  main.innerHTML =
    title(
      "Model evaluation",
      "Compare diagnosis accuracy, grounding and fallback coverage on controlled replays.",
    ) +
    '<section class="panel"><p class="muted">Loading saved benchmark…</p></section>';
  try {
    const report = await api("/evaluations/latest");
    if (page() !== "evaluation") return;
    main.innerHTML =
      title(
        "Model evaluation",
        "Controlled fixture replays · preview policy evidence",
      ) +
      `<section class="panel"><div class="title-row"><div><h2>Profile comparison</h2><small>${escape(report.generated_at || "No saved evaluation")} · ${escape(report.model || "")}</small></div><button class="primary" data-evaluate>Run evaluation</button></div><div class="toolbar"><label>Provider<select id="evaluation-provider"><option value="ollama">Local Ollama</option><option value="deterministic">Deterministic baseline only</option><option value="openai_compatible">Configured compatible endpoint</option></select></label><label>Model<input id="evaluation-model" value="llama3.2:1b" maxlength="128"></label></div><div id="evaluation-status" role="status"></div><p class="muted">Results measure this scenario set. Groundedness checks only the structured alert and diagnosis claims, not all model prose. Model coverage includes fallback detection; preview results do not establish execution safety.</p><div class="table-wrap"><table><thead><tr><th>PROFILE</th><th>STATUS</th><th>ACCURACY</th><th>GROUNDEDNESS</th><th>MODEL COVERAGE</th><th>CASES</th></tr></thead><tbody>${Object.entries(
        report.profiles || {},
      )
        .map(
          ([name, p]) =>
            `<tr><td>${escape(readable(name))}<br><small>${escape(p.reason || p.capabilities?.description || "")}</small></td><td>${badge(p.status)}</td><td>${percent(p.aggregates?.type_accuracy)}</td><td>${percent(p.aggregates?.avg_groundedness)}</td><td>${name === "deterministic_baseline" ? "N/A" : percent(p.model_coverage)}</td><td>${p.rows?.length || 0}</td></tr>`,
        )
        .join(
          "",
        )}</tbody></table></div>${evaluationCases(report.profiles)}${json("Comparison and limitations", report.hybrid_vs_baseline)}</section>`;
  } catch (error) {
    showError(error);
  }
}
async function refresh() {
  const route = location.hash;
  if (page() === "settings") {
    state.health = await api("/health");
    render();
    return;
  }
  const [health, runs, scenarios] = await Promise.all([
    api("/health"),
    api("/runs?limit=50"),
    api("/scenarios"),
  ]);
  if (location.hash !== route) return;
  Object.assign(state, { health, runs: runs.runs, scenarios: scenarios.items });
  document.querySelector("#connection").textContent =
    `${health.mcp_backend} backend · ${health.runtime_profile?.planner_provider}`;
  const id = location.hash.split("/")[1];
  if (id) {
    const run = await api(`/runs/${encodeURIComponent(id)}`);
    if (location.hash !== route) return;
    state.run = run;
  }
  render();
}
main.addEventListener("click", async (event) => {
  const citation = event.target.closest("[data-evidence]");
  if (citation) {
    event.preventDefault();
    document.getElementById(`evidence-${citation.dataset.evidence}`)?.scrollIntoView({block: "center"});
    return;
  }
  const button = event.target.closest("button");
  if (!button) return;
  try {
    if (button.hasAttribute("data-refresh")) await refresh();
    if (button.dataset.run) {
      state.step = 0;
      location.hash = `incidents/${button.dataset.run}`;
    }
    if (button.hasAttribute("data-back")) location.hash = "incidents";
    if (button.dataset.step !== undefined) {
      state.step = Number(button.dataset.step);
      renderRun();
    }
    if (button.hasAttribute("data-audit"))
      document.querySelector("#audit").innerHTML = json(
        "Recorded events",
        (await api(`/runs/${state.run.run_id}/audit`)).events,
      );
    if (button.hasAttribute("data-probe")) {
      button.disabled = true;
      document.querySelector("#probe").innerHTML = json(
        "Runtime health",
        await api("/platform/runtime-health?active_probe=true"),
      );
      button.disabled = false;
    }
    if (button.hasAttribute("data-evaluate")) {
      button.disabled = true;
      const provider = document.querySelector("#evaluation-provider").value;
      const model = document.querySelector("#evaluation-model").value.trim();
      const job = await api("/evaluations", {provider, model});
      state.evaluationStatus = job.status;
      document.querySelector("#evaluation-status").textContent =
        `Evaluation ${job.status}. You can continue investigating incidents.`;
    }
  } catch (error) {
    showError(error);
    button.disabled = false;
  }
});
main.addEventListener("submit", async (event) => {
  event.preventDefault();
  document.querySelector("#error").hidden = true;
  const form = event.target;
  try {
    if (form.id === "token-form") {
      sessionStorage.setItem("sre-token", new FormData(form).get("token"));
      await api("/scenarios");
      location.hash = "incidents";
      return;
    }
    if (state.busy) return;
    state.busy = true;
    if (form.id === "start-form") {
      const incident_id = new FormData(form).get("incident_id");
      renderIncidents();
      const run = await api("/runs/plan", {
        incident_id,
        execution_mode: "preview",
        wait_for_completion: false,
      });
      state.run = run;
      state.step = 0;
      location.hash = `incidents/${run.run_id}`;
    }
    if (form.id === "approve-form") {
      const execution_mode = form.querySelector("#mode").value;
      form.querySelector("button").disabled = true;
      state.run = await api(`/runs/${state.run.run_id}/approve-execute`, {
        execution_mode,
        proposal_hash: state.run.approval?.artifact?.proposal_hash,
      });
      state.step = 3;
      renderRun();
    }
  } catch (error) {
    showError(error);
  } finally {
    state.busy = false;
    if (form.id === "start-form" && page() === "incidents" && !location.hash.includes("/")) renderIncidents();
    if (form.isConnected) form.querySelectorAll("button").forEach(button => button.disabled = false);
  }
});
window.addEventListener("hashchange", () => refresh().catch(showError));
setInterval(async () => {
  if (page() === "evaluation") {
    try {
      const job = await api("/evaluations/status");
      const previous = state.evaluationStatus;
      state.evaluationStatus = job.status;
      const label = document.querySelector("#evaluation-status");
      if (label) label.textContent = job.status === "idle" ? "" : `Evaluation ${job.status}${job.status === "failed" ? ". See eval_history/workbench.log for the recorded error." : "."}`;
      const button = document.querySelector("[data-evaluate]");
      if (button) button.disabled = job.status === "running";
      if (previous === "running" && job.status === "complete") await renderEvaluation();
    } catch (error) { showError(error); }
    return;
  }
  if (state.busy || !location.hash.startsWith("#incidents/") || !state.run)
    return;
  if (
    ![
      "queued",
      "planning",
      "execution_queued",
      "executing",
      "verifying",
    ].includes(state.run.status)
  )
    return;
  try {
    const route = location.hash;
    const run = await api(`/runs/${state.run.run_id}`);
    if (location.hash !== route) return;
    state.run = run;
    renderRun();
  } catch (error) {
    showError(error);
  }
}, 2500);
refresh().catch((error) => {
  showError(error);
  main.innerHTML = title(
    "Connect to the runtime",
    "Open Runtime & access to enter your API token, then retry.",
  );
});
