import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, RefreshCw, XCircle } from "lucide-react";
import { getCustomerApprovalQueue, recordCustomerApproval } from "../lib/api";

export default function CustomerApprovalPage() {
  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(false);
  const [busyJobId, setBusyJobId] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const loadQueue = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await getCustomerApprovalQueue({ include_rework: true, limit: 200 });
      setJobs(data.jobs || []);
    } catch (queueError) {
      setError(queueError.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadQueue();
    const interval = setInterval(loadQueue, 8000);
    return () => clearInterval(interval);
  }, [loadQueue]);

  async function handleDecision(jobId, decision) {
    setBusyJobId(jobId);
    setError("");
    setMessage("");
    try {
      const data = await recordCustomerApproval(jobId, {
        decision,
        actor_id: "field_technician",
        notes: decision === "approve" ? "Customer approved." : "Customer declined.",
      });
      setMessage(`Updated ${jobId} -> ${data.status}`);
      await loadQueue();
    } catch (decisionError) {
      setError(decisionError.message);
    } finally {
      setBusyJobId("");
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Customer Approvals</h1>
          <p className="text-xs text-slate-500 mt-1">Simple list: pick a job, mark approved or declined.</p>
        </div>
        <button
          onClick={loadQueue}
          disabled={loading}
          className="border border-slate-700 hover:border-slate-500 px-3 py-2 rounded text-sm inline-flex items-center gap-2"
        >
          <RefreshCw size={14} />
          {loading ? "Refreshing..." : "Refresh"}
        </button>
      </div>

      {error && <div className="bg-red-900/20 border border-red-600/50 p-3 rounded text-red-200">{error}</div>}
      {message && <div className="bg-green-900/20 border border-green-600/50 p-3 rounded text-green-200">{message}</div>}

      <div className="bg-slate-900 border border-slate-800 rounded-xl">
        <div className="p-4 border-b border-slate-800 font-semibold">Pending Customer Decisions: {jobs.length}</div>
        {jobs.length === 0 && (
          <div className="p-4 text-sm text-slate-400">
            No jobs waiting on customer approval.
          </div>
        )}
        {jobs.map((job) => (
          <div key={job.job_id} className="p-4 border-b border-slate-800 last:border-b-0 space-y-2">
            <div className="font-mono text-xs text-slate-500">{job.job_id}</div>
            <div className="text-sm">
              <span className="text-slate-400">Equipment:</span> {job.equipment_id || "N/A"}
            </div>
            <div className="text-sm">
              <span className="text-slate-400">Fault:</span> {job.fault_code || "N/A"}
            </div>
            <div className="text-sm">
              <span className="text-slate-400">Symptoms:</span> {job.symptoms || "N/A"}
            </div>
            <div className="text-sm">
              <span className="text-slate-400">Customer:</span> {job.customer_name || "Unknown"}{" "}
              {job.customer_email ? `(${job.customer_email})` : ""}
            </div>
            <div className="text-sm">
              <span className="text-slate-400">Quote:</span>{" "}
              {job.quote_total_usd ? `$${Number(job.quote_total_usd).toFixed(2)}` : "N/A"}
            </div>
            <div className="flex gap-2 pt-1">
              <button
                onClick={() => handleDecision(job.job_id, "approve")}
                disabled={busyJobId === job.job_id}
                className="bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50 px-3 py-2 rounded text-sm font-semibold inline-flex items-center gap-1.5"
              >
                <CheckCircle2 size={14} />
                Approved
              </button>
              <button
                onClick={() => handleDecision(job.job_id, "deny")}
                disabled={busyJobId === job.job_id}
                className="bg-red-700 hover:bg-red-600 disabled:opacity-50 px-3 py-2 rounded text-sm font-semibold inline-flex items-center gap-1.5"
              >
                <XCircle size={14} />
                Declined
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
