import { useCallback, useEffect, useState } from "react";
import {
  HardDrive,
  LayoutDashboard,
  RefreshCw,
  Settings,
  UploadCloud,
  Wifi,
  Wrench,
} from "lucide-react";
import Link from "next/link";
import {
  APP_MODE_OFFLINE_1_3B,
  getAppMode,
  subscribeAppSettingsChanged,
} from "../../lib/appSettings";
import {
  API_BASE_URL,
  getApiBaseUrl,
  getOfflineQueueStatus,
  getRuntimeConfig,
  onOfflineQueueChanged,
  replayOfflineQueue,
} from "../../lib/api";

function toModeLabel(mode) {
  return mode === APP_MODE_OFFLINE_1_3B ? "OFFLINE 1-3B" : "ONLINE 8B";
}

export default function Layout({ children }) {
  const [isOnline, setIsOnline] = useState(() => {
    if (typeof window === "undefined") return true;
    return navigator.onLine;
  });
  const [queueCount, setQueueCount] = useState(0);
  const [replaying, setReplaying] = useState(false);
  const [replayMessage, setReplayMessage] = useState("");
  const [apiBaseUrl, setApiBaseUrl] = useState(() => getApiBaseUrl());
  const [appMode, setAppMode] = useState(() => getAppMode());
  const [runtimeModel, setRuntimeModel] = useState("N/A");
  const [runtimeTier, setRuntimeTier] = useState("N/A");

  const refreshRuntimeProof = useCallback(async () => {
    const mode = getAppMode();
    setAppMode(mode);
    try {
      const data = await getRuntimeConfig(mode === APP_MODE_OFFLINE_1_3B);
      setRuntimeModel(String(data?.model_selected || "N/A"));
      setRuntimeTier(String(data?.model_tier || "N/A"));
    } catch {
      setRuntimeModel("unreachable");
      setRuntimeTier("unverified");
    }
  }, []);

  const refreshQueueState = useCallback(async () => {
    try {
      const status = await getOfflineQueueStatus();
      setQueueCount(Number(status?.count || 0));
    } catch {
      setQueueCount(0);
    }
  }, []);

  const refreshSettingsState = useCallback(() => {
    setApiBaseUrl(getApiBaseUrl());
    setAppMode(getAppMode());
  }, []);

  useEffect(() => {
    const onlineHandler = () => {
      setIsOnline(true);
      refreshQueueState();
      refreshRuntimeProof();
    };
    const offlineHandler = () => {
      setIsOnline(false);
      refreshQueueState();
    };
    window.addEventListener("online", onlineHandler);
    window.addEventListener("offline", offlineHandler);
    return () => {
      window.removeEventListener("online", onlineHandler);
      window.removeEventListener("offline", offlineHandler);
    };
  }, [refreshQueueState, refreshRuntimeProof]);

  useEffect(() => {
    refreshQueueState();
    refreshSettingsState();
    refreshRuntimeProof();
    const unsubscribeQueue = onOfflineQueueChanged(refreshQueueState);
    const unsubscribeSettings = subscribeAppSettingsChanged(() => {
      refreshSettingsState();
      refreshRuntimeProof();
    });
    return () => {
      unsubscribeQueue();
      unsubscribeSettings();
    };
  }, [refreshQueueState, refreshRuntimeProof, refreshSettingsState]);

  async function handleReplayQueue() {
    setReplaying(true);
    setReplayMessage("");
    try {
      const result = await replayOfflineQueue();
      setReplayMessage(`Replayed ${result.synced}/${result.processed} queued action(s).`);
      await refreshQueueState();
      await refreshRuntimeProof();
    } catch (error) {
      setReplayMessage(String(error?.message || "Replay failed."));
    } finally {
      setReplaying(false);
    }
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col safe-left safe-right">
      <header className="safe-top bg-black border-b border-slate-800 p-4 flex flex-col md:flex-row md:justify-between md:items-center gap-3 sticky top-0 z-50">
        <div className="flex items-center gap-3">
          <div className="bg-red-600 p-1.5 rounded">
            <Wrench size={20} className="text-white" />
          </div>
          <span className="font-bold tracking-tighter text-lg">
            CUMMINS <span className="text-red-600">SERVICE</span>
          </span>
        </div>

        <div className="flex items-center gap-2 flex-wrap">
          <div
            className={`flex items-center gap-2 px-3 py-1 rounded-full border text-[10px] font-bold ${
              isOnline
                ? "border-green-500/30 bg-green-500/10 text-green-400"
                : "border-orange-500/30 bg-orange-500/10 text-orange-400"
            }`}
          >
            {isOnline ? <Wifi size={12} /> : <HardDrive size={12} />}
            {isOnline ? "NETWORK ONLINE" : "NETWORK OFFLINE"}
          </div>
          <div className="flex items-center gap-2 px-3 py-1 rounded-full border border-blue-700/40 bg-blue-900/20 text-[10px] font-bold text-blue-200">
            MODE: {toModeLabel(appMode)}
          </div>
          <div className="flex items-center gap-2 px-3 py-1 rounded-full border border-slate-700 bg-slate-900 text-[10px] font-bold text-slate-300">
            MODEL: {runtimeModel}
          </div>
          <div className="flex items-center gap-2 px-3 py-1 rounded-full border border-slate-700 bg-slate-900 text-[10px] font-bold text-slate-300">
            TIER: {runtimeTier}
          </div>
          <div className="flex items-center gap-2 px-3 py-1 rounded-full border border-slate-700 bg-slate-900 text-[10px] font-bold text-slate-300">
            <UploadCloud size={12} />
            QUEUED ACTIONS: {queueCount}
          </div>
          <button
            type="button"
            onClick={handleReplayQueue}
            disabled={replaying || queueCount === 0}
            className="px-3 py-1 rounded border border-slate-700 bg-slate-900 text-[10px] font-bold text-slate-200 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1"
          >
            <RefreshCw size={12} />
            {replaying ? "REPLAYING..." : "REPLAY QUEUE"}
          </button>
        </div>
      </header>

      <main className="flex-1 container mx-auto p-4 pb-28 md:pb-4">
        {replayMessage && (
          <div className="mb-3 text-xs border border-slate-800 rounded bg-slate-900 px-3 py-2 text-slate-300">
            {replayMessage}
          </div>
        )}
        <div className="text-[11px] text-slate-500 mb-3 space-y-1">
          <div>
            Backend: <span className="font-mono">{apiBaseUrl}</span>
          </div>
          <div>
            Default build URL: <span className="font-mono">{API_BASE_URL}</span>
          </div>
        </div>
        {children}
      </main>

      <nav className="safe-bottom fixed bottom-0 left-0 right-0 bg-black border-t border-slate-800 p-3 grid grid-cols-3 md:hidden">
        <Link href="/" className="flex flex-col items-center gap-1 text-slate-400 hover:text-red-500">
          <Wrench size={20} />
          <span className="text-[10px] font-bold uppercase">Job</span>
        </Link>
        <Link
          href="/supervisor"
          className="flex flex-col items-center gap-1 text-slate-400 hover:text-red-500"
        >
          <LayoutDashboard size={20} />
          <span className="text-[10px] font-bold uppercase">Supervisor</span>
        </Link>
        <Link
          href="/settings"
          className="flex flex-col items-center gap-1 text-slate-400 hover:text-red-500"
        >
          <Settings size={20} />
          <span className="text-[10px] font-bold uppercase">Settings</span>
        </Link>
      </nav>
    </div>
  );
}
