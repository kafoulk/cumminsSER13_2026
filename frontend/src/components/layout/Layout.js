import { useCallback, useEffect, useState } from "react";
import {
  HardDrive,
  LayoutDashboard,
  Settings,
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
      setReplayMessage(
        `Replayed ${result.synced}/${result.processed} queued action(s).`,
      );
      await refreshQueueState();
      await refreshRuntimeProof();
    } catch (error) {
      setReplayMessage(String(error?.message || "Replay failed."));
    } finally {
      setReplaying(false);
    }
  }

  return (
    <div className="min-h-screen bg-black text-slate-100 flex flex-col safe-left safe-right">
      {/* small header with proper spacing and vertical centering */}
      <header className="safe-top bg-white border-b border-slate-200 px-4 py-2 h-[56px] flex items-center justify-between sticky top-0 z-50">
        <div className="flex items-center gap-3">
          {/* logo sized to 35x35px */}
          <img
            src="/cummins-logo.svg"
            alt="Cummins logo"
            className="h-[35px] w-[35px]"
          />
          <span className="font-heading font-bold tracking-tighter text-lg text-black">
            Cummins <span className="text-cummins-red">Service</span>
          </span>
        </div>

        <div className="flex items-center">
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
        </div>
      </header>

      <main className="flex-1 container mx-auto p-4 pb-28 md:pb-4">
        {replayMessage && (
          <div className="mb-3 text-xs border border-slate-800 rounded bg-slate-900 px-3 py-2 text-slate-300">
            {replayMessage}
          </div>
        )}
        {children}
      </main>

      <nav className="safe-bottom fixed bottom-0 left-0 right-0 bg-black border-t border-slate-800 p-3 grid grid-cols-3 md:hidden">
        <Link
          href="/"
          className="flex flex-col items-center gap-1 text-slate-400 hover:text-red-500"
        >
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
