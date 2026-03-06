import { useEffect, useState } from "react";
import {
  APP_MODE_OFFLINE_1_3B,
  getApiBaseUrl,
  getAppMode,
  setApiBaseUrl,
} from "../lib/appSettings";
import { getHealth, getRuntimeConfig } from "../lib/api";

function modeLabel(mode) {
  return mode === APP_MODE_OFFLINE_1_3B ? "Offline (auto)" : "Online (auto)";
}

export default function SettingsPage() {
  const [apiBaseUrl, setApiBaseUrlInput] = useState(() => getApiBaseUrl());
  const [appMode, setAppModeState] = useState(() => getAppMode());
  const [runtimeInfo, setRuntimeInfo] = useState(null);
  const [healthInfo, setHealthInfo] = useState(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(false);

  async function loadDiagnostics(modeValue) {
    const useOffline = modeValue === APP_MODE_OFFLINE_1_3B;
    const runtime = await getRuntimeConfig(useOffline);
    const health = await getHealth(useOffline);
    setRuntimeInfo(runtime);
    setHealthInfo(health);
  }

  useEffect(() => {
    const syncModeFromNetwork = () => {
      setAppModeState(getAppMode());
    };
    window.addEventListener("online", syncModeFromNetwork);
    window.addEventListener("offline", syncModeFromNetwork);
    return () => {
      window.removeEventListener("online", syncModeFromNetwork);
      window.removeEventListener("offline", syncModeFromNetwork);
    };
  }, []);

  useEffect(() => {
    setLoading(true);
    setError("");
    loadDiagnostics(appMode)
      .catch((diagnosticError) => {
        setError(diagnosticError.message);
      })
      .finally(() => setLoading(false));
  }, [appMode]);

  function handleSave(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    const normalizedUrl = setApiBaseUrl(apiBaseUrl);
    setApiBaseUrlInput(normalizedUrl);
    setMessage("Backend URL saved. Mode selection is automatic based on connectivity.");
  }

  async function handleRefreshDiagnostics() {
    setLoading(true);
    setError("");
    setMessage("");
    try {
      const modeNow = getAppMode();
      setAppModeState(modeNow);
      await loadDiagnostics(modeNow);
      setMessage("Status refreshed.");
    } catch (diagnosticError) {
      setError(diagnosticError.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="space-y-6">
      <section className="bg-slate-900 border border-slate-800 p-4 rounded-xl space-y-4">
        <h1 className="text-xl font-bold">Mobile App Settings</h1>
        <p className="text-xs text-slate-400">
          Set your backend address here. Online/offline model switching happens automatically.
        </p>
        <form onSubmit={handleSave} className="space-y-3">
          <label className="space-y-1 block">
            <span className="text-xs text-slate-400">Backend Base URL</span>
            <input
              value={apiBaseUrl}
              onChange={(event) => setApiBaseUrlInput(event.target.value)}
              className="w-full bg-black border border-slate-700 p-2 rounded font-mono text-sm"
              placeholder="http://192.168.1.10:9054"
            />
          </label>
          <div className="text-xs text-slate-400">
            Current mode: <span className="font-mono">{modeLabel(appMode)}</span>
          </div>
          <div className="text-xs text-slate-500">
            Tip: on a physical phone, use your laptop LAN IP (for example `http://192.168.1.20:9054`).
          </div>
          <div className="flex gap-2">
            <button
              type="submit"
              className="bg-cummins-red hover:bg-red-700 transition px-4 py-2 rounded font-semibold text-sm"
            >
              Save Settings
            </button>
            <button
              type="button"
              onClick={handleRefreshDiagnostics}
              className="border border-slate-600 hover:border-slate-500 px-4 py-2 rounded text-sm"
              disabled={loading}
            >
              {loading ? "Checking..." : "Refresh Status"}
            </button>
          </div>
        </form>
      </section>

      {message && (
        <div className="bg-green-900/20 border border-green-600/50 p-3 rounded text-green-200">
          {message}
        </div>
      )}
      {error && (
        <div className="bg-red-900/20 border border-red-600/50 p-3 rounded text-red-200">
          {error}
        </div>
      )}

      <section className="bg-slate-900 border border-slate-800 p-4 rounded-xl space-y-2">
        <h2 className="font-semibold">Model Status</h2>
        <div className="text-sm text-slate-300">
          Current route: <span className="font-mono">{runtimeInfo?.mode_effective || "Not available"}</span>
        </div>
        <div className="text-sm text-slate-300">
          Model in use: <span className="font-mono">{runtimeInfo?.model_selected || "Not available"}</span>
        </div>
        <div className="text-sm text-slate-300">
          Model size: <span className="font-mono">{runtimeInfo?.model_tier || "Not available"}</span>
        </div>
        <div className="text-sm text-slate-300">
          Online model: <span className="font-mono">{runtimeInfo?.model_online || "Not available"}</span>
        </div>
        <div className="text-sm text-slate-300">
          Offline model: <span className="font-mono">{runtimeInfo?.model_offline || "Not available"}</span>
        </div>
      </section>

      <section className="bg-slate-900 border border-slate-800 p-4 rounded-xl space-y-2">
        <h2 className="font-semibold">Health Check</h2>
        <div className="text-sm text-slate-300">
          Service status: <span className="font-mono">{healthInfo?.status || "Not available"}</span>
        </div>
        <div className="text-sm text-slate-300">
          Last checked: <span className="font-mono">{healthInfo?.ts || "Not available"}</span>
        </div>
      </section>
    </div>
  );
}
