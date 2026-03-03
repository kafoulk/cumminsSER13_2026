import { useEffect, useMemo, useState } from "react";
import {
  getDemoScenarios,
  getJobDetails,
  getJobTimeline,
  getWorkflow,
  replanJob,
  submitJob,
  updateWorkflowStep,
} from "../../lib/api";

const defaultForm = {
  equipment_id: "EQ-1001",
  fault_code: "P0217",
  symptoms: "Engine temp rising under load",
  notes: "Coolant smell near radiator",
  location: "Indy Yard",
  is_offline: false,
  request_supervisor_review: false,
};

export default function TriageEngine() {
  const [form, setForm] = useState(defaultForm);
  const [result, setResult] = useState(null);
  const [jobDetails, setJobDetails] = useState(null);
  const [loading, setLoading] = useState(false);
  const [loadingDetails, setLoadingDetails] = useState(false);
  const [loadingWorkflow, setLoadingWorkflow] = useState(false);
  const [replanning, setReplanning] = useState(false);
  const [workflowSteps, setWorkflowSteps] = useState([]);
  const [workflowEvents, setWorkflowEvents] = useState([]);
  const [timeline, setTimeline] = useState([]);
  const [loadingTimeline, setLoadingTimeline] = useState(false);
  const [scenarioCatalog, setScenarioCatalog] = useState([]);
  const [selectedScenario, setSelectedScenario] = useState("");
  const [stepNotes, setStepNotes] = useState({});
  const [stepMeasurements, setStepMeasurements] = useState({});
  const [stepManualEscalation, setStepManualEscalation] = useState({});
  const [updatingStepId, setUpdatingStepId] = useState("");
  const [error, setError] = useState("");

  const createdJobId = useMemo(() => result?.job_id || "", [result]);

  useEffect(() => {
    async function loadScenarios() {
      try {
        const data = await getDemoScenarios();
        setScenarioCatalog(data.scenarios || []);
      } catch {
        setScenarioCatalog([]);
      }
    }
    loadScenarios();
  }, []);

  async function handleSubmit(event) {
    event.preventDefault();
    setLoading(true);
    setError("");
    setJobDetails(null);
    setTimeline([]);
    try {
      const data = await submitJob(form);
      setResult(data);
      setWorkflowSteps(data.initial_workflow || []);
      setWorkflowEvents([]);
    } catch (submitError) {
      setError(submitError.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleLoadJobDetails() {
    if (!createdJobId) return;
    setLoadingDetails(true);
    setError("");
    try {
      const data = await getJobDetails(createdJobId);
      setJobDetails(data);
      setWorkflowSteps(data.workflow_steps || []);
      setWorkflowEvents(data.workflow_events || []);
      await loadTimeline(createdJobId);
    } catch (detailsError) {
      setError(detailsError.message);
    } finally {
      setLoadingDetails(false);
    }
  }

  function updateField(field, value) {
    setForm((prev) => ({ ...prev, [field]: value }));
  }

  function loadScenarioById(scenarioId) {
    setSelectedScenario(scenarioId);
    const selected = scenarioCatalog.find((item) => item.id === scenarioId);
    if (!selected?.payload) return;
    setForm({
      equipment_id: selected.payload.equipment_id || "",
      fault_code: selected.payload.fault_code || "",
      symptoms: selected.payload.symptoms || "",
      notes: selected.payload.notes || "",
      location: selected.payload.location || "",
      is_offline: Boolean(selected.payload.is_offline),
      request_supervisor_review: Boolean(selected.payload.request_supervisor_review),
    });
  }

  async function loadTimeline(jobId) {
    if (!jobId) return;
    setLoadingTimeline(true);
    try {
      const data = await getJobTimeline(jobId);
      setTimeline(data.timeline || []);
    } catch (timelineError) {
      setError(timelineError.message);
    } finally {
      setLoadingTimeline(false);
    }
  }

  async function refreshWorkflow() {
    if (!createdJobId) return;
    setLoadingWorkflow(true);
    setError("");
    try {
      const data = await getWorkflow(createdJobId);
      setWorkflowSteps(data.workflow_steps || []);
      setWorkflowEvents(data.workflow_events || []);
      setResult((prev) =>
        prev
          ? {
              ...prev,
              status: data.status,
              requires_approval: data.requires_approval,
              escalation_reasons: data.escalation_reasons || [],
              risk_signals: data.risk_signals || prev.risk_signals,
              escalation_policy_version: data.escalation_policy_version || prev.escalation_policy_version,
            }
          : prev
      );
    } catch (workflowError) {
      setError(workflowError.message);
    } finally {
      setLoadingWorkflow(false);
    }
  }

  async function handleStepUpdate(stepId, status) {
    if (!createdJobId) return;
    setUpdatingStepId(stepId);
    setError("");
    try {
      const data = await updateWorkflowStep(createdJobId, {
        step_id: stepId,
        status,
        measurement_json: { value: stepMeasurements[stepId] || "" },
        notes: stepNotes[stepId] || "",
        actor_id: "field_technician",
        request_supervisor_review: Boolean(stepManualEscalation[stepId]),
      });
      setWorkflowSteps(data.workflow_steps || []);
      setResult((prev) =>
        prev
          ? {
              ...prev,
              status: data.status,
              requires_approval: data.requires_approval,
              escalation_reasons: data.escalation_reasons || [],
              risk_signals: data.risk_signals || prev.risk_signals,
              escalation_policy_version: data.escalation_policy_version || prev.escalation_policy_version,
            }
          : prev
      );
      await refreshWorkflow();
      await loadTimeline(createdJobId);
    } catch (stepError) {
      setError(stepError.message);
    } finally {
      setUpdatingStepId("");
    }
  }

  async function handleReplan() {
    if (!createdJobId) return;
    setReplanning(true);
    setError("");
    try {
      const data = await replanJob(createdJobId);
      setWorkflowSteps(data.updated_workflow || []);
      setResult((prev) =>
        prev
          ? {
              ...prev,
              status: data.status,
              requires_approval: data.requires_approval,
              escalation_reasons: data.escalation_reasons || [],
              risk_signals: data.risk_signals || prev.risk_signals,
              escalation_policy_version: data.escalation_policy_version || prev.escalation_policy_version,
              triage: data.triage,
              evidence: data.evidence,
              schedule_hint: data.schedule_hint,
            }
          : prev
      );
      await refreshWorkflow();
      await loadTimeline(createdJobId);
    } catch (replanError) {
      setError(replanError.message);
    } finally {
      setReplanning(false);
    }
  }

  return (
    <div className="space-y-6">
      <form onSubmit={handleSubmit} className="bg-slate-900 border border-slate-800 p-4 rounded-xl space-y-4">
        <h3 className="text-sm uppercase tracking-wide text-slate-400">Field Job Intake</h3>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <label className="space-y-1">
            <span className="text-xs text-slate-400">Equipment ID</span>
            <input
              value={form.equipment_id}
              onChange={(event) => updateField("equipment_id", event.target.value)}
              className="w-full bg-black border border-slate-700 p-2 rounded"
              required
            />
          </label>

          <label className="space-y-1">
            <span className="text-xs text-slate-400">Fault Code</span>
            <input
              value={form.fault_code}
              onChange={(event) => updateField("fault_code", event.target.value)}
              className="w-full bg-black border border-slate-700 p-2 rounded"
              required
            />
          </label>
        </div>

        <label className="space-y-1 block">
          <span className="text-xs text-slate-400">Symptoms</span>
          <textarea
            value={form.symptoms}
            onChange={(event) => updateField("symptoms", event.target.value)}
            className="w-full bg-black border border-slate-700 p-2 rounded min-h-[80px]"
            required
          />
        </label>

        <label className="space-y-1 block">
          <span className="text-xs text-slate-400">Notes</span>
          <textarea
            value={form.notes}
            onChange={(event) => updateField("notes", event.target.value)}
            className="w-full bg-black border border-slate-700 p-2 rounded min-h-[80px]"
            required
          />
        </label>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <label className="space-y-1">
            <span className="text-xs text-slate-400">Location</span>
            <input
              value={form.location}
              onChange={(event) => updateField("location", event.target.value)}
              className="w-full bg-black border border-slate-700 p-2 rounded"
            />
          </label>

          <label className="flex items-center gap-2 mt-6 text-sm">
            <input
              type="checkbox"
              checked={form.is_offline}
              onChange={(event) => updateField("is_offline", event.target.checked)}
            />
            Force offline for this job
          </label>
          <label className="flex items-center gap-2 mt-6 text-sm">
            <input
              type="checkbox"
              checked={form.request_supervisor_review}
              onChange={(event) => updateField("request_supervisor_review", event.target.checked)}
            />
            Request supervisor review (manual)
          </label>
        </div>

        <button
          type="submit"
          disabled={loading}
          className="bg-cummins-red hover:bg-red-700 transition px-4 py-2 rounded font-semibold disabled:opacity-50"
        >
          {loading ? "Submitting..." : "Submit Job"}
        </button>
        <div className="pt-2 grid grid-cols-1 md:grid-cols-[1fr_auto] gap-2 items-end">
          <label className="space-y-1">
            <span className="text-xs text-slate-400">Demo scenario</span>
            <select
              value={selectedScenario}
              onChange={(event) => loadScenarioById(event.target.value)}
              className="w-full bg-black border border-slate-700 p-2 rounded text-sm"
            >
              <option value="">Select a canned scenario...</option>
              {scenarioCatalog.map((scenario) => (
                <option key={scenario.id} value={scenario.id}>
                  {scenario.label}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            onClick={() => loadScenarioById("safety_escalation")}
            className="border border-orange-600 text-orange-300 hover:bg-orange-950/30 px-4 py-2 rounded font-semibold"
          >
            Quick Load Safety Scenario
          </button>
        </div>
      </form>

      {error && (
        <div className="bg-red-900/20 border border-red-500/50 text-red-300 p-3 rounded">
          {error}
        </div>
      )}

      {result && (
        <section className="bg-slate-900 border border-slate-800 p-4 rounded-xl space-y-3">
          <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
            <h3 className="font-bold text-lg">Service Report</h3>
            <div className="text-xs text-slate-300">
              Job: <span className="font-mono">{result.job_id}</span> | Status:{" "}
              <span className="font-semibold">{result.status}</span>
            </div>
          </div>

          <pre className="whitespace-pre-wrap text-sm bg-black/40 border border-slate-800 rounded p-3">
            {result.service_report}
          </pre>

          {result.requires_approval ? (
            <div className="bg-orange-900/20 border border-orange-600/60 text-orange-200 p-3 rounded text-sm">
              This job escalated to supervisor review and should appear in the Supervisor Queue.
            </div>
          ) : (
            <div className="bg-sky-900/20 border border-sky-600/50 text-sky-200 p-3 rounded text-sm">
              This job did not escalate. Only `PENDING_APPROVAL` jobs appear in Supervisor Queue.
            </div>
          )}

          {Array.isArray(result.escalation_reasons) && result.escalation_reasons.length > 0 && (
            <div className="bg-slate-800/60 border border-slate-700 text-slate-200 p-3 rounded text-sm">
              Escalation reasons: {result.escalation_reasons.join(", ")}
            </div>
          )}

          <div className="bg-black/30 border border-slate-800 rounded p-3 text-sm space-y-1">
            <div className="text-xs uppercase tracking-wide text-slate-400">Escalation Decision</div>
            <div>
              Policy version:{" "}
              <span className="font-mono text-slate-300">
                {result.escalation_policy_version || "N/A"}
              </span>
            </div>
            <div>
              Risk source:{" "}
              <span className="font-semibold text-slate-200">
                {result.risk_signals?.source || "N/A"}
              </span>
              {" | "}confidence: {result.risk_signals?.confidence ?? "N/A"}
            </div>
            <div>Safety signal: {String(Boolean(result.risk_signals?.safety_signal))}</div>
            <div>Warranty signal: {String(Boolean(result.risk_signals?.warranty_signal))}</div>
            <div className="text-xs text-slate-400">
              Matched safety terms: {(result.risk_signals?.matched_terms?.safety || []).join(", ") || "none"}
            </div>
            <div className="text-xs text-slate-400">
              Matched warranty terms: {(result.risk_signals?.matched_terms?.warranty || []).join(", ") || "none"}
            </div>
            <div className="text-xs text-slate-400">
              Rationale: {result.risk_signals?.rationale || "No rationale available"}
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <div className="bg-black/30 border border-slate-800 rounded p-3 text-sm">
              <div className="text-xs uppercase text-red-400 mb-2">Agent 1: Triage</div>
              <div className="text-slate-300">Confidence: {result.triage?.confidence}</div>
              <div className="text-slate-300 mt-1">{result.triage?.summary}</div>
            </div>
            <div className="bg-black/30 border border-slate-800 rounded p-3 text-sm">
              <div className="text-xs uppercase text-red-400 mb-2">Agent 2: Parts / Evidence</div>
              <div className="text-slate-300">Confidence: {result.evidence?.confidence}</div>
              <div className="text-slate-300 mt-1">
                Parts: {(result.evidence?.parts_candidates || []).join(", ")}
              </div>
            </div>
            <div className="bg-black/30 border border-slate-800 rounded p-3 text-sm">
              <div className="text-xs uppercase text-red-400 mb-2">Agent 3: Scheduler</div>
              <div className="text-slate-300">
                Priority: {result.schedule_hint?.priority_hint || "N/A"}
              </div>
              <div className="text-slate-300 mt-1">ETA: {result.schedule_hint?.eta_bucket || "N/A"}</div>
            </div>
          </div>

          <button
            onClick={handleLoadJobDetails}
            disabled={loadingDetails}
            className="border border-slate-600 hover:border-slate-500 px-3 py-2 rounded text-sm"
          >
            {loadingDetails ? "Loading job details..." : "Load Full Job + Decision Log"}
          </button>
          <button
            onClick={refreshWorkflow}
            disabled={loadingWorkflow}
            className="border border-slate-600 hover:border-slate-500 px-3 py-2 rounded text-sm ml-2"
          >
            {loadingWorkflow ? "Refreshing workflow..." : "Refresh Workflow"}
          </button>
          <button
            onClick={handleReplan}
            disabled={replanning}
            className="border border-purple-700 text-purple-200 hover:bg-purple-950/30 px-3 py-2 rounded text-sm ml-2"
          >
            {replanning ? "Replanning..." : "Replan Job"}
          </button>
          <button
            onClick={() => loadTimeline(createdJobId)}
            disabled={loadingTimeline}
            className="border border-slate-600 hover:border-slate-500 px-3 py-2 rounded text-sm ml-2"
          >
            {loadingTimeline ? "Loading timeline..." : "Load Timeline"}
          </button>
        </section>
      )}

      {workflowSteps.length > 0 && (
        <section className="bg-slate-900 border border-slate-800 p-4 rounded-xl space-y-3">
          <h3 className="font-bold text-lg">Actionable Workflow</h3>
          <p className="text-xs text-slate-400">
            Update each step as done/blocked/failed. High-risk failures auto-escalate to supervisor queue.
          </p>
          <div className="space-y-3">
            {workflowSteps.map((step) => (
              <div key={step.step_id} className="border border-slate-800 rounded p-3 space-y-2">
                <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
                  <div>
                    <div className="text-sm font-semibold">
                      {step.step_order}. {step.title}
                    </div>
                    <div className="text-xs text-slate-400">{step.instructions}</div>
                  </div>
                  <div className="text-xs font-mono">
                    risk={step.risk_level} | status={step.status}
                  </div>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                  <input
                    value={stepMeasurements[step.step_id] || ""}
                    onChange={(event) =>
                      setStepMeasurements((prev) => ({
                        ...prev,
                        [step.step_id]: event.target.value,
                      }))
                    }
                    placeholder="Measurement value (optional)"
                    className="bg-black border border-slate-700 p-2 rounded text-sm"
                  />
                  <input
                    value={stepNotes[step.step_id] || ""}
                    onChange={(event) =>
                      setStepNotes((prev) => ({
                        ...prev,
                        [step.step_id]: event.target.value,
                      }))
                    }
                    placeholder="Notes (optional)"
                    className="bg-black border border-slate-700 p-2 rounded text-sm"
                  />
                  <label className="flex items-center gap-2 text-xs text-slate-300 p-2">
                    <input
                      type="checkbox"
                      checked={Boolean(stepManualEscalation[step.step_id])}
                      onChange={(event) =>
                        setStepManualEscalation((prev) => ({
                          ...prev,
                          [step.step_id]: event.target.checked,
                        }))
                      }
                    />
                    Request supervisor review on this step
                  </label>
                </div>
                <div className="flex gap-2">
                  <button
                    disabled={updatingStepId === step.step_id}
                    onClick={() => handleStepUpdate(step.step_id, "done")}
                    className="bg-green-700 hover:bg-green-600 px-3 py-1 rounded text-xs font-semibold"
                  >
                    Done
                  </button>
                  <button
                    disabled={updatingStepId === step.step_id}
                    onClick={() => handleStepUpdate(step.step_id, "blocked")}
                    className="bg-yellow-700 hover:bg-yellow-600 px-3 py-1 rounded text-xs font-semibold"
                  >
                    Blocked
                  </button>
                  <button
                    disabled={updatingStepId === step.step_id}
                    onClick={() => handleStepUpdate(step.step_id, "failed")}
                    className="bg-red-700 hover:bg-red-600 px-3 py-1 rounded text-xs font-semibold"
                  >
                    Failed
                  </button>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {workflowEvents.length > 0 && (
        <section className="bg-slate-900 border border-slate-800 p-4 rounded-xl space-y-2">
          <h3 className="font-bold text-lg">Workflow Events</h3>
          <div className="max-h-64 overflow-auto border border-slate-800 rounded">
            {workflowEvents.map((event) => (
              <div key={event.id} className="p-3 border-b border-slate-800 last:border-b-0 text-xs">
                <div className="font-mono text-slate-400">
                  {event.ts} | {event.actor_id} | {event.event_type}
                </div>
                <div>
                  step: {event.step_id || "N/A"} | output: {JSON.stringify(event.output_json || {})}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {jobDetails?.decision_log?.length > 0 && (
        <section className="bg-slate-900 border border-slate-800 p-4 rounded-xl space-y-2">
          <h3 className="font-bold text-lg">Decision Log (Canonical Audit Trail)</h3>
          <div className="max-h-80 overflow-auto border border-slate-800 rounded">
            {jobDetails.decision_log.map((entry) => (
              <div key={entry.id} className="p-3 border-b border-slate-800 last:border-b-0 text-sm">
                <div className="font-mono text-xs text-slate-400">
                  {entry.ts} | {entry.agent_id} | {entry.action}
                </div>
                <div className="text-slate-200">confidence: {entry.confidence}</div>
              </div>
            ))}
          </div>
        </section>
      )}

      {timeline.length > 0 && (
        <section className="bg-slate-900 border border-slate-800 p-4 rounded-xl space-y-2">
          <h3 className="font-bold text-lg">Unified Timeline</h3>
          <div className="max-h-80 overflow-auto border border-slate-800 rounded">
            {timeline.map((event) => (
              <div
                key={`${event.kind}-${event.event_id}-${event.ts}`}
                className="p-3 border-b border-slate-800 last:border-b-0 text-xs"
              >
                <div className="font-mono text-slate-400">
                  {event.ts} | {event.kind} | {event.actor_id} | {event.event_name}
                </div>
                {event.step_id && <div>step: {event.step_id}</div>}
                {typeof event.confidence === "number" && <div>confidence: {event.confidence}</div>}
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
