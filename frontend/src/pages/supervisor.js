import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, PlusCircle, RefreshCw, ShieldCheck } from "lucide-react";
import {
  adjustPartInventory,
  approveJob,
  getApiBaseUrl,
  getJobDetails,
  getPartsInventory,
  getPartsRestockRequests,
  getSupervisorQueue,
  getSupervisorTickets,
  replenishPartInventory,
  replayOfflineQueue,
  syncOfflineQueue,
  upsertPartCatalog,
} from "../lib/api";

function stockTone(status) {
  const normalized = String(status || "").toUpperCase();
  if (normalized === "OUT_OF_STOCK") return "text-red-300 border-red-700 bg-red-900/20";
  if (normalized === "LOW_STOCK") return "text-amber-300 border-amber-700 bg-amber-900/20";
  return "text-emerald-300 border-emerald-700 bg-emerald-900/20";
}

export default function Supervisor() {
  const [activeMenu, setActiveMenu] = useState("approvals");
  const [approverName, setApproverName] = useState("Supervisor A");
  const [queue, setQueue] = useState([]);
  const [notesByJob, setNotesByJob] = useState({});
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [busyJobId, setBusyJobId] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [queueSourceMessage, setQueueSourceMessage] = useState("");
  const [lastRefreshTs, setLastRefreshTs] = useState("");

  const [ticketStateFilter, setTicketStateFilter] = useState("ALL");
  const [ticketLedger, setTicketLedger] = useState([]);
  const [ticketCounts, setTicketCounts] = useState({ open: 0, closed: 0 });
  const [loadingTickets, setLoadingTickets] = useState(false);

  const [attachmentsByJob, setAttachmentsByJob] = useState({});
  const [loadingAttachmentsJobId, setLoadingAttachmentsJobId] = useState("");

  const [partsQuery, setPartsQuery] = useState("");
  const [partsLocation, setPartsLocation] = useState("");
  const [partsInventory, setPartsInventory] = useState([]);
  const [loadingParts, setLoadingParts] = useState(false);
  const [restockRequests, setRestockRequests] = useState([]);
  const [loadingRestock, setLoadingRestock] = useState(false);
  const [busyRestockRequestId, setBusyRestockRequestId] = useState("");
  const [adjustingPartKey, setAdjustingPartKey] = useState("");

  const [newPartName, setNewPartName] = useState("");
  const [newPartCategory, setNewPartCategory] = useState("general");
  const [newPartLocation, setNewPartLocation] = useState("");
  const [newPartInitialQty, setNewPartInitialQty] = useState("5");

  const allTasks = Number(ticketCounts.open || 0) + Number(ticketCounts.closed || 0);
  const pendingTasks = Number(ticketCounts.open || 0);
  const pendingApprovals = Number(queue.length || 0);
  const sortedPartLocations = useMemo(() => {
    const values = new Set();
    for (const item of partsInventory) {
      const location = String(item?.location || "").trim();
      if (location) values.add(location);
    }
    return Array.from(values).sort((a, b) => a.localeCompare(b));
  }, [partsInventory]);

  function minutesPending(updatedTs) {
    if (!updatedTs) return 0;
    const updated = Date.parse(updatedTs);
    if (Number.isNaN(updated)) return 0;
    return Math.max(0, Math.floor((Date.now() - updated) / 60000));
  }

  function toAttachmentUrl(contentUrl) {
    const raw = String(contentUrl || "").trim();
    if (!raw) return "";
    if (raw.startsWith("http://") || raw.startsWith("https://")) return raw;
    return `${getApiBaseUrl()}${raw}`;
  }

  const loadQueue = useCallback(async () => {
    setLoading(true);
    setError("");
    setQueueSourceMessage("");
    try {
      const data = await getSupervisorQueue();
      setQueue(data.jobs || []);
      if (data.local_only) {
        setQueueSourceMessage(
          data.detail || "Backend unreachable. Showing on-device queued supervisor items.",
        );
      }
      setLastRefreshTs(new Date().toLocaleTimeString());
    } catch (queueError) {
      setError(queueError.message);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadTicketLedger = useCallback(async () => {
    setLoadingTickets(true);
    try {
      const data = await getSupervisorTickets({
        ticket_state: ticketStateFilter,
        limit: 200,
      });
      setTicketLedger(data.tickets || []);
      setTicketCounts({
        open: Number(data.open_count || 0),
        closed: Number(data.closed_count || 0),
      });
    } catch {
      setTicketLedger([]);
      setTicketCounts({ open: 0, closed: 0 });
    } finally {
      setLoadingTickets(false);
    }
  }, [ticketStateFilter]);

  const loadPartsInventory = useCallback(async () => {
    setLoadingParts(true);
    try {
      const data = await getPartsInventory({
        q: partsQuery,
        location: partsLocation,
        limit: 800,
      });
      setPartsInventory(data.items || []);
    } catch {
      setPartsInventory([]);
    } finally {
      setLoadingParts(false);
    }
  }, [partsQuery, partsLocation]);

  const loadRestockRequests = useCallback(async () => {
    setLoadingRestock(true);
    try {
      const data = await getPartsRestockRequests({ status: "PENDING", limit: 200 });
      setRestockRequests(data.requests || []);
    } catch {
      setRestockRequests([]);
    } finally {
      setLoadingRestock(false);
    }
  }, []);

  useEffect(() => {
    loadQueue();
    const interval = setInterval(() => {
      loadQueue();
    }, 8000);
    return () => clearInterval(interval);
  }, [loadQueue]);

  useEffect(() => {
    loadTicketLedger();
    const interval = setInterval(() => {
      loadTicketLedger();
    }, 10000);
    return () => clearInterval(interval);
  }, [loadTicketLedger]);

  useEffect(() => {
    loadPartsInventory();
  }, [loadPartsInventory]);

  useEffect(() => {
    loadRestockRequests();
    const interval = setInterval(() => {
      loadRestockRequests();
    }, 10000);
    return () => clearInterval(interval);
  }, [loadRestockRequests]);

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
      if (result.queued_offline) {
        setMessage(
          "Decision saved offline. It will sync when network is available.",
        );
      } else {
        setMessage(`Updated ${jobId} to ${result.status}.`);
      }
      await loadQueue();
      await loadTicketLedger();
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
      const localReplay = await replayOfflineQueue();
      let serverResult = null;
      let serverError = "";
      try {
        serverResult = await syncOfflineQueue();
      } catch (syncError) {
        serverError = syncError.message;
      }

      if (serverResult) {
        setMessage(
          `Local replay ${localReplay.synced}/${localReplay.processed}. Cloud sync processed=${serverResult.processed}, synced=${serverResult.synced}, failed=${serverResult.failed}.`,
        );
      } else {
        setMessage(
          `Local replay ${localReplay.synced}/${localReplay.processed}. Cloud sync pending (${serverError || "backend unreachable"}).`,
        );
      }
      await loadQueue();
      await loadTicketLedger();
      await loadPartsInventory();
      await loadRestockRequests();
    } catch (syncError) {
      setError(syncError.message);
    } finally {
      setSyncing(false);
    }
  }

  async function handleLoadAttachments(jobId) {
    setLoadingAttachmentsJobId(jobId);
    setError("");
    try {
      const details = await getJobDetails(jobId);
      setAttachmentsByJob((prev) => ({
        ...prev,
        [jobId]: details.attachments || [],
      }));
    } catch (attachmentError) {
      setError(attachmentError.message);
    } finally {
      setLoadingAttachmentsJobId("");
    }
  }

  async function handleAddPart(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    if (!newPartName.trim()) {
      setError("Part name is required.");
      return;
    }
    try {
      const result = await upsertPartCatalog({
        part_name: newPartName.trim(),
        category: newPartCategory.trim() || "general",
        unit: "each",
        location: newPartLocation.trim() || null,
        initial_quantity: Number(newPartInitialQty || 0),
        actor_id: approverName.trim() || "Supervisor",
        actor_role: "supervisor",
      });
      setMessage(`Saved part ${result?.part?.part_name || newPartName}.`);
      setNewPartName("");
      await loadPartsInventory();
    } catch (partError) {
      setError(partError.message);
    }
  }

  async function handleFulfillRestock(request) {
    setBusyRestockRequestId(String(request.request_id));
    setError("");
    setMessage("");
    try {
      const result = await replenishPartInventory({
        part_id: request.part_id,
        location: request.location,
        quantity_add: Number(request.requested_qty || 1),
        request_id: request.request_id,
        actor_id: approverName.trim() || "Supervisor",
        actor_role: "supervisor",
      });
      setMessage(
        `Fulfilled restock ${request.request_id} (${result?.inventory?.part_name || request.part_id}).`,
      );
      await loadPartsInventory();
      await loadRestockRequests();
    } catch (restockError) {
      setError(restockError.message);
    } finally {
      setBusyRestockRequestId("");
    }
  }

  async function handleAdjustPart(item, delta) {
    if (!item?.part_id || !item?.location || !delta) return;
    const key = `${item.part_id}:${item.location}:${delta}`;
    setAdjustingPartKey(key);
    setError("");
    setMessage("");
    try {
      const result = await adjustPartInventory({
        part_id: item.part_id,
        location: item.location,
        quantity_delta: Number(delta),
        actor_id: approverName.trim() || "Supervisor",
        actor_role: "supervisor",
      });
      const nextQty = result?.inventory?.quantity_on_hand;
      const deltaLabel = Number(delta) > 0 ? `+${delta}` : `${delta}`;
      setMessage(`Adjusted ${item.part_name} (${deltaLabel}). New qty: ${nextQty}.`);
      await loadPartsInventory();
    } catch (adjustError) {
      setError(adjustError.message);
    } finally {
      setAdjustingPartKey("");
    }
  }

  async function handleRefreshActiveTab() {
    if (activeMenu === "approvals") {
      await loadQueue();
      return;
    }
    if (activeMenu === "parts") {
      await loadPartsInventory();
      await loadRestockRequests();
      return;
    }
    await loadTicketLedger();
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Supervisor Workspace</h1>
          <p className="text-xs text-slate-500 mt-1">
            Auto-refresh is active. Last refresh: {lastRefreshTs || "not yet"}
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={handleRefreshActiveTab}
            disabled={loading || loadingParts || loadingTickets || loadingRestock}
            className="border border-slate-700 hover:border-slate-500 px-3 py-2 rounded text-sm flex items-center gap-2"
          >
            <RefreshCw size={14} />
            {loading || loadingParts || loadingTickets || loadingRestock
              ? "Refreshing..."
              : "Refresh"}
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

      <section className="bg-slate-900 border border-slate-800 p-3 rounded-xl">
        <div className="text-xs uppercase tracking-wide text-slate-500 mb-2">
          Supervisor Menu
        </div>
        <div className="grid grid-cols-3 gap-2">
          {[
            { id: "approvals", label: "Pending Approval" },
            { id: "parts", label: "Parts" },
            { id: "ledger", label: "Ticket Ledger" },
          ].map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => setActiveMenu(item.id)}
              className={`px-3 py-2 rounded text-xs font-semibold border ${
                activeMenu === item.id
                  ? "bg-cummins-red/20 border-cummins-red text-white"
                  : "bg-black/20 border-slate-700 text-slate-300 hover:border-slate-500"
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
      </section>

      <div className="bg-slate-900 border border-slate-800 p-3 rounded-xl">
        <label className="text-xs text-slate-400 block mb-1">Approver Name</label>
        <input
          value={approverName}
          onChange={(event) => setApproverName(event.target.value)}
          className="w-full md:w-80 bg-black border border-slate-700 p-2 rounded"
          placeholder="Supervisor Name"
        />
      </div>

      <div className="bg-slate-900 border border-slate-800 p-3 rounded-xl">
        <div className="text-xs uppercase tracking-wide text-slate-500 mb-2">Taskbar</div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
          <div className="bg-black/30 border border-slate-800 rounded p-3">
            <div className="text-[11px] uppercase tracking-wide text-slate-500">All Tasks</div>
            <div className="text-2xl font-semibold text-slate-100">{allTasks}</div>
          </div>
          <div className="bg-amber-900/20 border border-amber-700/60 rounded p-3">
            <div className="text-[11px] uppercase tracking-wide text-amber-300">Pending Tasks</div>
            <div className="text-2xl font-semibold text-amber-200">{pendingTasks}</div>
          </div>
          <div className="bg-cyan-900/20 border border-cyan-700/60 rounded p-3">
            <div className="text-[11px] uppercase tracking-wide text-cyan-300">Pending Approvals</div>
            <div className="text-2xl font-semibold text-cyan-200">{pendingApprovals}</div>
          </div>
        </div>
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

      {activeMenu === "approvals" && queueSourceMessage && (
        <div className="bg-amber-900/20 border border-amber-600/50 p-3 rounded text-amber-100">
          {queueSourceMessage}
        </div>
      )}

      {activeMenu === "approvals" && (
      <div className="bg-slate-900 border border-slate-800 rounded-xl">
        <div className="p-4 border-b border-slate-800 font-semibold">
          Pending Approvals: {queue.length}
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
                <span className="text-slate-400">Location:</span> {job.location || "Not provided"}
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
                <span className="text-slate-400">Why this needs review:</span>{" "}
                {(job.escalation_reasons || []).join(", ") || "No reason listed"}
              </div>
              <div>
                <span className="text-slate-400">Current stage:</span>{" "}
                {job.approval_stage || "waiting for approval"}
              </div>
              <div>
                <span className="text-slate-400">Attachments:</span> {job.attachment_count || 0}
              </div>
              <div className="text-xs text-slate-400">
                {job.workflow_intent ||
                  "Diagnostic checklist is in progress and waiting for supervisor decision."}
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

            <button
              onClick={() => handleLoadAttachments(job.job_id)}
              disabled={loadingAttachmentsJobId === job.job_id}
              className="border border-slate-700 hover:border-slate-500 disabled:opacity-50 px-3 py-2 rounded text-xs"
            >
              {loadingAttachmentsJobId === job.job_id ? "Loading images..." : "View Images"}
            </button>

            {(attachmentsByJob[job.job_id] || []).length > 0 && (
              <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                {(attachmentsByJob[job.job_id] || []).map((attachment) => (
                  <a
                    key={attachment.attachment_id}
                    href={toAttachmentUrl(attachment.content_url)}
                    target="_blank"
                    rel="noreferrer"
                    className="block border border-slate-800 rounded p-2 bg-black/30"
                  >
                    <img
                      src={toAttachmentUrl(attachment.content_url)}
                      alt={attachment.caption || attachment.filename || "Attachment"}
                      className="w-full h-24 object-cover rounded"
                    />
                    <div className="text-[11px] text-slate-300 truncate mt-1">
                      {attachment.filename || attachment.attachment_id}
                    </div>
                  </a>
                ))}
              </div>
            )}

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
      )}

      {activeMenu === "parts" && (
        <div className="space-y-4">
          <section className="bg-slate-900 border border-slate-800 rounded-xl p-3 grid grid-cols-1 md:grid-cols-3 gap-2">
            <input
              value={partsQuery}
              onChange={(event) => setPartsQuery(event.target.value)}
              className="bg-black border border-slate-700 p-2 rounded text-sm"
              placeholder="Search part/category/id"
            />
            <select
              value={partsLocation}
              onChange={(event) => setPartsLocation(event.target.value)}
              className="bg-black border border-slate-700 p-2 rounded text-sm"
            >
              <option value="">All locations</option>
              {sortedPartLocations.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
            <div className="text-xs text-slate-400 flex items-center">
              Rows: {partsInventory.length}
            </div>
          </section>

          <section className="bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-3">
            <div className="font-semibold text-sm inline-flex items-center gap-2">
              <PlusCircle size={14} />
              Add New Part
            </div>
            <form onSubmit={handleAddPart} className="grid grid-cols-1 md:grid-cols-4 gap-2">
              <input
                value={newPartName}
                onChange={(event) => setNewPartName(event.target.value)}
                className="bg-black border border-slate-700 p-2 rounded text-sm"
                placeholder="New part name"
              />
              <input
                value={newPartCategory}
                onChange={(event) => setNewPartCategory(event.target.value)}
                className="bg-black border border-slate-700 p-2 rounded text-sm"
                placeholder="Category"
              />
              <input
                value={newPartLocation}
                onChange={(event) => setNewPartLocation(event.target.value)}
                className="bg-black border border-slate-700 p-2 rounded text-sm"
                placeholder="Initial location (optional)"
              />
              <div className="flex gap-2">
                <input
                  value={newPartInitialQty}
                  onChange={(event) => setNewPartInitialQty(event.target.value)}
                  className="bg-black border border-slate-700 p-2 rounded text-sm w-24"
                  placeholder="Qty"
                />
                <button
                  type="submit"
                  className="bg-cummins-red hover:bg-red-700 px-3 py-2 rounded text-xs font-semibold"
                >
                  Add Part
                </button>
              </div>
            </form>
          </section>

          <section className="bg-slate-900 border border-slate-800 rounded-xl">
            <div className="p-4 border-b border-slate-800 font-semibold text-sm">
              Pending Restock Requests ({restockRequests.length})
            </div>
            {loadingRestock && (
              <div className="p-4 text-sm text-slate-400">Loading restock requests...</div>
            )}
            {!loadingRestock && restockRequests.length === 0 && (
              <div className="p-4 text-sm text-slate-400">No pending requests.</div>
            )}
            {!loadingRestock &&
              restockRequests.map((request) => (
                <div
                  key={request.request_id}
                  className="p-4 border-b border-slate-800 last:border-b-0 space-y-2"
                >
                  <div className="text-xs font-mono text-slate-500">{request.request_id}</div>
                  <div className="text-sm text-slate-200">
                    {request.part_name_snapshot} | {request.location} | qty {request.requested_qty}
                  </div>
                  <div className="text-xs text-slate-400">
                    Requested by {request.requested_by} ({request.requested_role})
                  </div>
                  <button
                    onClick={() => handleFulfillRestock(request)}
                    disabled={busyRestockRequestId === request.request_id}
                    className="bg-emerald-800 hover:bg-emerald-700 disabled:opacity-50 px-3 py-1 rounded text-xs font-semibold"
                  >
                    {busyRestockRequestId === request.request_id ? "Fulfilling..." : "Fulfill Request"}
                  </button>
                </div>
              ))}
          </section>

          <section className="bg-slate-900 border border-slate-800 rounded-xl">
            <div className="p-4 border-b border-slate-800 font-semibold text-sm">
              Inventory ({partsInventory.length})
            </div>
            {loadingParts && (
              <div className="p-4 text-sm text-slate-400">Loading parts...</div>
            )}
            {!loadingParts && partsInventory.length === 0 && (
              <div className="p-4 text-sm text-slate-400">No inventory rows found.</div>
            )}
            {!loadingParts &&
              partsInventory.map((item) => (
                <div
                  key={`${item.part_id}-${item.location}`}
                  className="p-4 border-b border-slate-800 last:border-b-0 space-y-1"
                >
                  <div className="text-sm font-semibold text-slate-200">{item.part_name}</div>
                  <div className="text-xs text-slate-500 font-mono">{item.part_id}</div>
                  <div className="text-xs text-slate-400">
                    {item.category} | {item.location}
                  </div>
                  <div className="flex items-center gap-2">
                    <span className={`px-2 py-0.5 rounded border text-[11px] ${stockTone(item.stock_status)}`}>
                      {item.stock_status}
                    </span>
                    <span className="text-xs text-slate-300">
                      Qty: {item.quantity_on_hand} (reorder {item.reorder_level})
                    </span>
                  </div>
                  <div className="pt-1 flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => handleAdjustPart(item, -1)}
                      disabled={adjustingPartKey === `${item.part_id}:${item.location}:-1`}
                      className="px-2 py-1 rounded border border-slate-700 hover:border-slate-500 text-xs font-semibold disabled:opacity-50"
                    >
                      {adjustingPartKey === `${item.part_id}:${item.location}:-1` ? "..." : "-1"}
                    </button>
                    <button
                      type="button"
                      onClick={() => handleAdjustPart(item, 1)}
                      disabled={adjustingPartKey === `${item.part_id}:${item.location}:1`}
                      className="px-2 py-1 rounded border border-slate-700 hover:border-slate-500 text-xs font-semibold disabled:opacity-50"
                    >
                      {adjustingPartKey === `${item.part_id}:${item.location}:1` ? "..." : "+1"}
                    </button>
                  </div>
                </div>
              ))}
          </section>
        </div>
      )}

      {activeMenu === "ledger" && (
      <div className="bg-slate-900 border border-slate-800 rounded-xl">
        <div className="p-4 border-b border-slate-800 flex flex-col md:flex-row md:items-center md:justify-between gap-3">
          <div>
            <div className="font-semibold">Ticket Ledger (Supervisor Only)</div>
            <div className="text-xs text-slate-500 mt-1">
              Open: {ticketCounts.open} | Closed: {ticketCounts.closed}
            </div>
          </div>
          <div className="flex gap-2">
            {["ALL", "OPEN", "CLOSED"].map((state) => (
              <button
                key={state}
                type="button"
                onClick={() => setTicketStateFilter(state)}
                className={`px-3 py-1.5 rounded text-xs border ${
                  ticketStateFilter === state
                    ? "bg-cummins-red/20 border-cummins-red text-white"
                    : "bg-black/20 border-slate-700 text-slate-300 hover:border-slate-500"
                }`}
              >
                {state}
              </button>
            ))}
          </div>
        </div>
        {loadingTickets && (
          <div className="p-4 text-slate-400 text-sm">Loading ticket ledger...</div>
        )}
        {!loadingTickets && ticketLedger.length === 0 && (
          <div className="p-4 text-slate-400 text-sm">No tickets found for this filter.</div>
        )}
        {!loadingTickets &&
          ticketLedger.map((ticket) => (
            <div key={`ledger-${ticket.job_id}`} className="p-4 border-b border-slate-800 last:border-b-0 text-sm">
              <div className="font-mono text-xs text-slate-400">{ticket.job_id}</div>
              <div className="mt-1">
                <span className="text-slate-400">Status:</span> {ticket.status} |{" "}
                <span className={ticket.ticket_state === "CLOSED" ? "text-emerald-300" : "text-amber-300"}>
                  {ticket.ticket_state}
                </span>
              </div>
              <div>
                <span className="text-slate-400">Equipment:</span> {ticket.equipment_id || "N/A"} |{" "}
                <span className="text-slate-400">Fault:</span> {ticket.fault_code || "N/A"}
              </div>
              <div>
                <span className="text-slate-400">Customer:</span>{" "}
                {ticket.customer_name || ticket.customer_email || ticket.customer_phone || "N/A"}
              </div>
              <div>
                <span className="text-slate-400">Location:</span> {ticket.location || "N/A"} |{" "}
                <span className="text-slate-400">Tech:</span> {ticket.assigned_tech_id || "unassigned"}
              </div>
              <div>
                <span className="text-slate-400">Updated:</span> {ticket.updated_ts || "N/A"} |{" "}
                <span className="text-slate-400">Age:</span> {minutesPending(ticket.updated_ts)} min
              </div>
              {ticket.ticket_state === "CLOSED" && (
                <div className="text-xs text-slate-400 mt-1">
                  Closed: {ticket.closed_ts || "N/A"} ({ticket.close_reason || "closed"})
                </div>
              )}
            </div>
          ))}
      </div>
      )}
    </div>
  );
}
