import { useCallback, useEffect, useMemo, useState } from "react";
import { RefreshCw, PlusCircle, PackageCheck } from "lucide-react";
import {
  adjustPartInventory,
  getPartsInventory,
  getPartsRestockRequests,
  replenishPartInventory,
  upsertPartCatalog,
} from "../lib/api";
import { AUTH_ROLE_SUPERVISOR, getAuthSession } from "../lib/authSession";

function stockTone(status) {
  const normalized = String(status || "").toUpperCase();
  if (normalized === "OUT_OF_STOCK") return "text-red-300 border-red-700 bg-red-900/20";
  if (normalized === "LOW_STOCK") return "text-amber-300 border-amber-700 bg-amber-900/20";
  return "text-emerald-300 border-emerald-700 bg-emerald-900/20";
}

export default function PartsPage() {
  const session = getAuthSession();
  const actorRole = String(session?.role || "technician");
  const actorId = String(session?.display_name || "Supervisor");
  const isSupervisor = actorRole === AUTH_ROLE_SUPERVISOR;

  const [location, setLocation] = useState("");
  const [query, setQuery] = useState("");
  const [inventory, setInventory] = useState([]);
  const [loading, setLoading] = useState(false);
  const [restockLoading, setRestockLoading] = useState(false);
  const [restockRequests, setRestockRequests] = useState([]);
  const [busyRequestId, setBusyRequestId] = useState("");
  const [adjustingKey, setAdjustingKey] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const [newPartName, setNewPartName] = useState("");
  const [newPartCategory, setNewPartCategory] = useState("general");
  const [newPartLocation, setNewPartLocation] = useState("");
  const [newPartInitialQty, setNewPartInitialQty] = useState("5");
  const [replenishPartId, setReplenishPartId] = useState("");
  const [replenishLocation, setReplenishLocation] = useState("");
  const [replenishQty, setReplenishQty] = useState("5");

  const sortedLocations = useMemo(() => {
    const set = new Set();
    for (const item of inventory) {
      const value = String(item?.location || "").trim();
      if (value) set.add(value);
    }
    return Array.from(set).sort((a, b) => a.localeCompare(b));
  }, [inventory]);

  const loadInventory = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await getPartsInventory({ location, q: query, limit: 800 });
      setInventory(data.items || []);
    } catch (inventoryError) {
      setError(inventoryError.message);
      setInventory([]);
    } finally {
      setLoading(false);
    }
  }, [location, query]);

  const loadRestockRequests = useCallback(async () => {
    if (!isSupervisor) return;
    setRestockLoading(true);
    try {
      const data = await getPartsRestockRequests({ status: "PENDING", limit: 200 });
      setRestockRequests(data.requests || []);
    } catch {
      setRestockRequests([]);
    } finally {
      setRestockLoading(false);
    }
  }, [isSupervisor]);

  useEffect(() => {
    loadInventory();
  }, [loadInventory]);

  useEffect(() => {
    loadRestockRequests();
  }, [loadRestockRequests]);

  async function handleAddPart(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    if (!newPartName.trim()) {
      setError("Part name is required.");
      return;
    }
    try {
      const payload = {
        part_name: newPartName.trim(),
        category: newPartCategory.trim() || "general",
        unit: "each",
        location: newPartLocation.trim() || null,
        initial_quantity: Number(newPartInitialQty || 0),
        actor_id: actorId,
        actor_role: actorRole,
      };
      const data = await upsertPartCatalog(payload);
      setMessage(`Saved part ${data?.part?.part_name || newPartName}.`);
      setNewPartName("");
      await loadInventory();
    } catch (addError) {
      setError(addError.message);
    }
  }

  async function handleReplenish(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    if (!replenishPartId.trim() || !replenishLocation.trim()) {
      setError("Part ID and location are required.");
      return;
    }
    try {
      const payload = {
        part_id: replenishPartId.trim(),
        location: replenishLocation.trim(),
        quantity_add: Number(replenishQty || 0),
        actor_id: actorId,
        actor_role: actorRole,
      };
      const data = await replenishPartInventory(payload);
      setMessage(
        `Replenished ${data?.inventory?.part_name || replenishPartId} at ${data?.inventory?.location}.`,
      );
      await loadInventory();
      await loadRestockRequests();
    } catch (replenishError) {
      setError(replenishError.message);
    }
  }

  async function handleFulfillRequest(request) {
    setBusyRequestId(String(request.request_id));
    setError("");
    setMessage("");
    try {
      const data = await replenishPartInventory({
        part_id: request.part_id,
        location: request.location,
        quantity_add: Number(request.requested_qty || 1),
        request_id: request.request_id,
        actor_id: actorId,
        actor_role: actorRole,
      });
      setMessage(
        `Fulfilled restock request ${request.request_id} (${data?.inventory?.part_name || request.part_id}).`,
      );
      await loadInventory();
      await loadRestockRequests();
    } catch (fulfillError) {
      setError(fulfillError.message);
    } finally {
      setBusyRequestId("");
    }
  }

  async function handleAdjustQuantity(item, quantityDelta) {
    const delta = Number(quantityDelta || 0);
    if (!isSupervisor || !item?.part_id || !item?.location || delta === 0) return;
    const key = `${item.part_id}:${item.location}:${delta}`;
    setAdjustingKey(key);
    setError("");
    setMessage("");
    try {
      const data = await adjustPartInventory({
        part_id: item.part_id,
        location: item.location,
        quantity_delta: delta,
        actor_id: actorId,
        actor_role: actorRole,
      });
      const nextQty = data?.inventory?.quantity_on_hand;
      const changeLabel = delta > 0 ? `+${delta}` : String(delta);
      setMessage(
        `Adjusted ${item.part_name} at ${item.location} (${changeLabel}). New qty: ${nextQty}.`,
      );
      await loadInventory();
    } catch (adjustError) {
      setError(adjustError.message);
    } finally {
      setAdjustingKey("");
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Parts Inventory</h1>
          <p className="text-xs text-slate-500 mt-1">
            Synthetic stock by location. Technicians consume; supervisors can add or subtract.
          </p>
        </div>
        <button
          onClick={() => {
            loadInventory();
            loadRestockRequests();
          }}
          disabled={loading || restockLoading}
          className="border border-slate-700 hover:border-slate-500 px-3 py-2 rounded text-sm inline-flex items-center gap-2"
        >
          <RefreshCw size={14} />
          {loading || restockLoading ? "Refreshing..." : "Refresh"}
        </button>
      </div>

      <div className="bg-slate-900 border border-slate-800 rounded-xl p-3 grid grid-cols-1 md:grid-cols-3 gap-2">
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          className="bg-black border border-slate-700 p-2 rounded text-sm"
          placeholder="Search part/category/id"
        />
        <select
          value={location}
          onChange={(event) => setLocation(event.target.value)}
          className="bg-black border border-slate-700 p-2 rounded text-sm"
        >
          <option value="">All locations</option>
          {sortedLocations.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
        <div className="text-xs text-slate-400 flex items-center">
          Rows: {inventory.length}
        </div>
      </div>

      {error && (
        <div className="bg-red-900/20 border border-red-600/50 p-3 rounded text-red-200">
          {error}
        </div>
      )}
      {message && (
        <div className="bg-green-900/20 border border-green-600/50 p-3 rounded text-green-200">
          {message}
        </div>
      )}

      {isSupervisor && (
        <section className="bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-3">
          <div className="font-semibold text-sm inline-flex items-center gap-2">
            <PlusCircle size={14} />
            Supervisor Controls
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
          <form onSubmit={handleReplenish} className="grid grid-cols-1 md:grid-cols-4 gap-2">
            <input
              value={replenishPartId}
              onChange={(event) => setReplenishPartId(event.target.value)}
              className="bg-black border border-slate-700 p-2 rounded text-sm"
              placeholder="part_id"
            />
            <input
              value={replenishLocation}
              onChange={(event) => setReplenishLocation(event.target.value)}
              className="bg-black border border-slate-700 p-2 rounded text-sm"
              placeholder="Location"
            />
            <input
              value={replenishQty}
              onChange={(event) => setReplenishQty(event.target.value)}
              className="bg-black border border-slate-700 p-2 rounded text-sm"
              placeholder="Quantity to add"
            />
            <button
              type="submit"
              className="bg-slate-800 border border-slate-700 hover:border-slate-500 px-3 py-2 rounded text-xs font-semibold"
            >
              Replenish
            </button>
          </form>
        </section>
      )}

      {isSupervisor && (
        <section className="bg-slate-900 border border-slate-800 rounded-xl">
          <div className="p-4 border-b border-slate-800 font-semibold text-sm">
            Pending Restock Requests ({restockRequests.length})
          </div>
          {restockRequests.length === 0 ? (
            <div className="p-4 text-sm text-slate-400">No pending requests.</div>
          ) : (
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
                  onClick={() => handleFulfillRequest(request)}
                  disabled={busyRequestId === request.request_id}
                  className="bg-emerald-800 hover:bg-emerald-700 disabled:opacity-50 px-3 py-1 rounded text-xs font-semibold inline-flex items-center gap-1"
                >
                  <PackageCheck size={13} />
                  {busyRequestId === request.request_id ? "Fulfilling..." : "Fulfill Request"}
                </button>
              </div>
            ))
          )}
        </section>
      )}

      <section className="bg-slate-900 border border-slate-800 rounded-xl">
        <div className="p-4 border-b border-slate-800 font-semibold text-sm">
          Inventory ({inventory.length})
        </div>
        {inventory.length === 0 ? (
          <div className="p-4 text-sm text-slate-400">No inventory rows found.</div>
        ) : (
          inventory.map((item) => (
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
              {isSupervisor && (
                <div className="pt-1 flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => handleAdjustQuantity(item, -1)}
                    disabled={adjustingKey === `${item.part_id}:${item.location}:-1`}
                    className="px-2 py-1 rounded border border-slate-700 hover:border-slate-500 text-xs font-semibold disabled:opacity-50"
                  >
                    {adjustingKey === `${item.part_id}:${item.location}:-1` ? "..." : "-1"}
                  </button>
                  <button
                    type="button"
                    onClick={() => handleAdjustQuantity(item, 1)}
                    disabled={adjustingKey === `${item.part_id}:${item.location}:1`}
                    className="px-2 py-1 rounded border border-slate-700 hover:border-slate-500 text-xs font-semibold disabled:opacity-50"
                  >
                    {adjustingKey === `${item.part_id}:${item.location}:1` ? "..." : "+1"}
                  </button>
                </div>
              )}
            </div>
          ))
        )}
      </section>
    </div>
  );
}
