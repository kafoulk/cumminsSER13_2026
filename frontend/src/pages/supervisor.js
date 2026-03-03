import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, RefreshCw, ShieldCheck } from "lucide-react";
import {
  approveJob,
  getAgentMetrics,
  getSupervisorQueue,
  syncOfflineQueue,
} from "../lib/api";

export default function Supervisor() {
  const [approverName, setApproverName] = useState("Supervisor A");
  const [queue, setQueue] = useState([]);
  const [notesByJob, setNotesByJob] = useState({});
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [busyJobId, setBusyJobId] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [lastRefreshTs, setLastRefreshTs] = useState("");
  const [metrics, setMetrics] = useState([]);

  function minutesPending(updatedTs) {
    if (!updatedTs) return 0;
    const updated = Date.parse(updatedTs);
    if (Number.isNaN(updated)) return 0;
    return Math.max(0, Math.floor((Date.now() - updated) / 60000));
  }

  const loadQueue = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await getSupervisorQueue();
      setQueue(data.jobs || []);
      setLastRefreshTs(new Date().toLocaleTimeString());
    } catch (queueError) {
      setError(queueError.message);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadMetrics = useCallback(async () => {
    try {
      const data = await getAgentMetrics();
      setMetrics(data.metrics || []);
    } catch {
      setMetrics([]);
    }
  }, []);

  useEffect(() => {
    loadMetrics();
    const interval = setInterval(loadMetrics, 12000);
    return () => clearInterval(interval);
  }, [loadMetrics]);

  useEffect(() => {
    loadQueue();
    const interval = setInterval(() => {
      loadQueue();
    }, 8000);
    return () => clearInterval(interval);
  }, [loadQueue]);

  async function handleDecision(jobId, decision) {
    if (!approverName.trim()) {
      setError("Approver name is required.");
      return;
    }
    setBusyJobId(jobId);
    setError("");
    setMessage("");
    try {
      const payload = {
        job_id: jobId,
        approver_name: approverName.trim(),
        decision,
        notes: notesByJob[jobId] || "",
      };
      const result = await approveJob(payload);
      setMessage(`Updated ${jobId} to ${result.status}.`);
      await loadQueue();
      await loadMetrics();
    } catch (decisionError) {
      setError(decisionError.message);
    } finally {
      setBusyJobId("");
    }
  }

  async function handleSync() {
    setSyncing(true);
    setError("");
    setMessage("");
    try {
      const result = await syncOfflineQueue();
      setMessage(
        `Sync complete. processed=${result.processed}, synced=${result.synced}, failed=${result.failed}`
      );
      await loadQueue();
      await loadMetrics();
    } catch (syncError) {
      setError(syncError.message);
    } finally {
      setSyncing(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Supervisor Approval Queue</h1>
          <p className="text-xs text-slate-500 mt-1">
            Auto-refresh every 8s. Last refresh: {lastRefreshTs || "not yet"}
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={loadQueue}
            disabled={loading}
            className="border border-slate-700 hover:border-slate-500 px-3 py-2 rounded text-sm flex items-center gap-2"
          >
            <RefreshCw size={14} />
            {loading ? "Refreshing..." : "Refresh Queue"}
          </button>
          <button
            onClick={handleSync}
            disabled={syncing}
            className="bg-slate-800 border border-slate-700 hover:border-slate-500 px-3 py-2 rounded text-sm flex items-center gap-2"
          >
            <ShieldCheck size={14} />
            {syncing ? "Syncing..." : "Run Sync"}
          </button>
        </div>
      </div>

      <div className="bg-slate-900 border border-slate-800 p-3 rounded-xl">
        <label className="text-xs text-slate-400 block mb-1">Approver Name</label>
        <input
          value={approverName}
          onChange={(event) => setApproverName(event.target.value)}
          className="w-full md:w-80 bg-black border border-slate-700 p-2 rounded"
          placeholder="Supervisor Name"
        />
      </div>

      {error && (
        <div className="bg-red-900/20 border border-red-600/50 p-3 rounded text-red-200 flex items-center gap-2">
          <AlertTriangle size={16} />
          {error}
        </div>
      )}

      {message && (
        <div className="bg-green-900/20 border border-green-600/50 p-3 rounded text-green-200">
          {message}
        </div>
      )}

      <div className="bg-slate-900 border border-slate-800 rounded-xl">
        <div className="p-4 border-b border-slate-800 font-semibold">Agent Performance (Local Metrics)</div>
        {metrics.length === 0 && (
          <div className="p-4 text-slate-400 text-sm">No metrics yet.</div>
        )}
        {metrics.slice(0, 6).map((item) => (
          <div key={`${item.day}-${item.agent_id}`} className="p-3 border-b border-slate-800 last:border-b-0 text-sm">
            <div className="font-mono text-xs text-slate-400">
              {item.day} | {item.agent_id}
            </div>
            <div>
              jobs={item.jobs_processed} escalations={item.escalations} approvals={item.approvals} denials=
              {item.denials} replans={item.replans} mean_conf={Number(item.mean_confidence).toFixed(2)}
            </div>
          </div>
        ))}
      </div>

      <div className="bg-slate-900 border border-slate-800 rounded-xl">
        <div className="p-4 border-b border-slate-800 font-semibold">
          Pending Jobs: {queue.length}
        </div>
        {queue.length === 0 && (
          <div className="p-4 text-slate-400 text-sm">No pending approvals.</div>
        )}
        {queue.map((job) => (
          <div key={job.job_id} className="p-4 border-b border-slate-800 last:border-b-0 space-y-3">
            <div className="text-sm space-y-1">
              <div className="font-mono text-xs text-slate-400">{job.job_id}</div>
              <div>
                <span className="text-slate-400">Equipment:</span> {job.equipment_id}
              </div>
              <div>
                <span className="text-slate-400">Fault:</span> {job.fault_code}
              </div>
              <div>
                <span className="text-slate-400">Symptoms:</span> {job.symptoms}
              </div>
              <div>
                <span className="text-slate-400">Location:</span> {job.location || "N/A"}
              </div>
              <div>
                <span className="text-slate-400">High-risk failed steps:</span>{" "}
                {job.high_risk_failed_steps}
              </div>
              <div>
                <span className="text-slate-400">Pending age:</span> {minutesPending(job.updated_ts)} min
                {minutesPending(job.updated_ts) >= 10 && (
                  <span className="ml-2 text-red-300 font-semibold">SLA breach</span>
                )}
              </div>
              <div>
                <span className="text-slate-400">Escalation reasons:</span>{" "}
                {(job.escalation_reasons || []).join(", ") || "N/A"}
              </div>
              <div>
                <span className="text-slate-400">Risk source:</span>{" "}
                {job.risk_signals?.source || "N/A"} | confidence={job.risk_signals?.confidence ?? "N/A"}
              </div>
              <div className="text-xs text-slate-400">
                Matched safety terms: {(job.risk_signals?.matched_terms?.safety || []).join(", ") || "none"}
              </div>
              <div className="text-xs text-slate-400">
                Matched warranty terms:{" "}
                {(job.risk_signals?.matched_terms?.warranty || []).join(", ") || "none"}
              </div>
            </div>

            <textarea
              value={notesByJob[job.job_id] || ""}
              onChange={(event) =>
                setNotesByJob((prev) => ({ ...prev, [job.job_id]: event.target.value }))
              }
              className="w-full bg-black border border-slate-700 p-2 rounded text-sm min-h-[70px]"
              placeholder="Optional approval notes"
            />

            <div className="flex gap-2">
              <button
                onClick={() => handleDecision(job.job_id, "approve")}
                disabled={busyJobId === job.job_id}
                className="bg-green-700 hover:bg-green-600 disabled:opacity-50 px-3 py-2 rounded text-sm font-semibold"
              >
                Approve
              </button>
              <button
                onClick={() => handleDecision(job.job_id, "deny")}
                disabled={busyJobId === job.job_id}
                className="bg-red-700 hover:bg-red-600 disabled:opacity-50 px-3 py-2 rounded text-sm font-semibold"
              >
                Deny
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
