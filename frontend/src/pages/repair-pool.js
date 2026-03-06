import { useCallback, useEffect, useMemo, useState } from "react";
import { CheckCircle2, RefreshCw, Wrench } from "lucide-react";
import { claimRepairJob, completeRepairJob, getRepairPool } from "../lib/api";
import { getAuthSession } from "../lib/authSession";

function deriveTechId() {
  const session = getAuthSession();
  const raw = String(session?.display_name || "tech-001")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return raw || "tech-001";
}

export default function RepairPoolPage() {
  const [jobs, setJobs] = useState([]);
  const [includeClaimed, setIncludeClaimed] = useState(true);
  const [loading, setLoading] = useState(false);
  const [busyJobId, setBusyJobId] = useState("");
  const [notesByJob, setNotesByJob] = useState({});
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [technicianId, setTechnicianId] = useState(() => deriveTechId());

  const technicianName = useMemo(() => {
    const session = getAuthSession();
    return String(session?.display_name || technicianId);
  }, [technicianId]);

  const loadPool = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await getRepairPool({ include_claimed: includeClaimed });
      setJobs(data.jobs || []);
    } catch (poolError) {
      setError(poolError.message);
    } finally {
      setLoading(false);
    }
  }, [includeClaimed]);

  useEffect(() => {
    loadPool();
    const interval = setInterval(loadPool, 8000);
    return () => clearInterval(interval);
  }, [loadPool]);

  async function handleClaim(jobId) {
    setBusyJobId(jobId);
    setError("");
    setMessage("");
    try {
      const data = await claimRepairJob(jobId, {
        technician_id: technicianId,
        technician_name: technicianName,
      });
      setMessage(`Claimed ${jobId} (${data.status}).`);
      await loadPool();
    } catch (claimError) {
      setError(claimError.message);
    } finally {
      setBusyJobId("");
    }
  }

  async function handleComplete(jobId) {
    setBusyJobId(jobId);
    setError("");
    setMessage("");
    try {
      const data = await completeRepairJob(jobId, {
        technician_id: technicianId,
        notes: notesByJob[jobId] || "",
      });
      setMessage(`Completed ${jobId} (${data.status}).`);
      await loadPool();
    } catch (completeError) {
      setError(completeError.message);
    } finally {
      setBusyJobId("");
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Repair Pool</h1>
          <p className="text-xs text-slate-500 mt-1">Open customer-approved tickets ready to claim.</p>
        </div>
        <button
          onClick={loadPool}
          disabled={loading}
          className="border border-slate-700 hover:border-slate-500 px-3 py-2 rounded text-sm inline-flex items-center gap-2"
        >
          <RefreshCw size={14} />
          {loading ? "Refreshing..." : "Refresh"}
        </button>
      </div>

      <div className="bg-slate-900 border border-slate-800 rounded-xl p-3 space-y-3">
        <label className="text-xs text-slate-400 block">Technician ID</label>
        <input
          value={technicianId}
          onChange={(event) => setTechnicianId(event.target.value)}
          className="w-full bg-black border border-slate-700 p-2 rounded"
          placeholder="tech-001"
        />
        <label className="inline-flex items-center gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={includeClaimed}
            onChange={(event) => setIncludeClaimed(event.target.checked)}
          />
          Show claimed jobs
        </label>
      </div>

      {error && <div className="bg-red-900/20 border border-red-600/50 p-3 rounded text-red-200">{error}</div>}
      {message && <div className="bg-green-900/20 border border-green-600/50 p-3 rounded text-green-200">{message}</div>}

      <div className="bg-slate-900 border border-slate-800 rounded-xl">
        <div className="p-4 border-b border-slate-800 font-semibold">Tickets: {jobs.length}</div>
        {jobs.length === 0 && <div className="p-4 text-sm text-slate-400">No jobs in repair pool.</div>}
        {jobs.map((job) => {
          const canClaim = job.status === "REPAIR_POOL_OPEN";
          const canComplete = job.status === "REPAIR_IN_PROGRESS";
          return (
            <div key={job.job_id} className="p-4 border-b border-slate-800 last:border-b-0 space-y-3">
              <div className="text-xs font-mono text-slate-500">{job.job_id}</div>
              <div className="text-sm">
                <span className="text-slate-400">Equipment:</span> {job.equipment_id || "N/A"}
              </div>
              <div className="text-sm">
                <span className="text-slate-400">Fault:</span> {job.fault_code || "N/A"}
              </div>
              <div className="text-sm">
                <span className="text-slate-400">Status:</span> {job.status}
              </div>
              <div className="text-sm">
                <span className="text-slate-400">Workflow steps:</span> {job.workflow_step_count || 0}
              </div>
              <div className="text-sm">
                <span className="text-slate-400">Quote total:</span>{" "}
                {job.quote_total_usd ? `$${Number(job.quote_total_usd).toFixed(2)}` : "N/A"}
              </div>
              <textarea
                value={notesByJob[job.job_id] || ""}
                onChange={(event) =>
                  setNotesByJob((prev) => ({
                    ...prev,
                    [job.job_id]: event.target.value,
                  }))
                }
                className="w-full bg-black border border-slate-700 rounded p-2 text-sm"
                rows={2}
                placeholder="Completion notes (optional)"
              />
              <div className="flex gap-2">
                <button
                  onClick={() => handleClaim(job.job_id)}
                  disabled={!canClaim || busyJobId === job.job_id}
                  className={`px-3 py-2 rounded text-sm inline-flex items-center gap-2 ${
                    canClaim
                      ? "bg-slate-800 border border-slate-700 hover:border-slate-500"
                      : "bg-slate-900 border border-slate-800 text-slate-500"
                  }`}
                >
                  <Wrench size={14} />
                  Claim
                </button>
                <button
                  onClick={() => handleComplete(job.job_id)}
                  disabled={!canComplete || busyJobId === job.job_id}
                  className={`px-3 py-2 rounded text-sm inline-flex items-center gap-2 ${
                    canComplete
                      ? "bg-green-900/25 border border-green-700 hover:border-green-500"
                      : "bg-slate-900 border border-slate-800 text-slate-500"
                  }`}
                >
                  <CheckCircle2 size={14} />
                  Complete
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
