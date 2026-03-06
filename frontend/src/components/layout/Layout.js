import { useCallback, useEffect, useState } from "react";
import {
  HardDrive,
  ListChecks,
  LayoutDashboard,
  MailCheck,
  LogOut,
  PackageSearch,
  Settings,
  Wifi,
  Wrench,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/router";
import {
  APP_MODE_OFFLINE_1_3B,
  getAppMode,
  subscribeAppSettingsChanged,
} from "../../lib/appSettings";
import {
  AUTH_ROLE_SUPERVISOR,
  clearAuthSession,
  getAuthSession,
  normalizeRoutePath,
  subscribeAuthSessionChanged,
} from "../../lib/authSession";
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

function toRoleLabel(role) {
  return role === AUTH_ROLE_SUPERVISOR ? "SUPERVISOR" : "TECHNICIAN";
}

export default function Layout({ children }) {
  const router = useRouter();
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
  const [authSession, setAuthSession] = useState(() => getAuthSession());
  const currentPath = normalizeRoutePath(router.asPath || router.pathname);
  const onLoginPage = currentPath === "/login";
  const navItems =
    authSession?.role === AUTH_ROLE_SUPERVISOR
      ? [
          { href: "/supervisor", label: "Supervisor", icon: LayoutDashboard },
          { href: "/parts", label: "Parts", icon: PackageSearch },
          { href: "/settings", label: "Settings", icon: Settings },
        ]
      : [
          { href: "/", label: "Job", icon: Wrench },
          { href: "/parts", label: "Parts", icon: PackageSearch },
          { href: "/customer-approval", label: "Customer OK", icon: MailCheck },
          { href: "/repair-pool", label: "Repair Pool", icon: ListChecks },
          { href: "/settings", label: "Settings", icon: Settings },
        ];

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

  useEffect(() => {
    setAuthSession(getAuthSession());
    const unsubscribe = subscribeAuthSessionChanged(() => {
      setAuthSession(getAuthSession());
    });
    return () => unsubscribe();
  }, []);

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

  function handleSignOut() {
    clearAuthSession();
    router.replace("/login");
  }

  return (
    <div className="min-h-screen bg-black text-slate-100 flex flex-col safe-left safe-right">
      <header className="safe-top bg-black/80 backdrop-blur border-b border-white/10 px-4 min-h-[64px] flex items-center justify-between sticky top-0 z-50">
      <div className="flex items-center gap-2.5">
        <img
          src="/cummins-logo.svg"
          alt="Cummins logo"
          className="h-9 w-9 shrink-0 rounded-md bg-white p-1"
        />
       <span className="inline-flex items-baseline gap-1 font-heading font-bold tracking-tighter text-lg leading-none text-white">
            <span>Cummins</span>
            <span className="relative top-[1px] text-cummins-red">Service</span>
       </span>
      </div>

        <div className="flex items-center gap-2">
          <div
            className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-[10px] font-medium ${
            isOnline
              ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-300"
              : "border-amber-400/30 bg-amber-400/10 text-amber-200"
            }`}
          >
            {isOnline ? <Wifi size={12} /> : <HardDrive size={12} />}
            <span className="tracking-wide">{isOnline ? "ONLINE" : "OFFLINE"}</span>
          </div>

          {!onLoginPage && authSession && (
            <button
              onClick={handleSignOut}
              className="inline-flex items-center gap-1.5 px-2 py-1 rounded border border-slate-700 text-[10px] text-slate-300 hover:border-slate-500"
              title={`Signed in as ${authSession.display_name}`}
            >
              <LogOut size={11} />
              <span className="max-w-[90px] truncate">{authSession.display_name}</span>
              <span className="uppercase text-slate-400 hidden sm:inline">
                {toRoleLabel(authSession.role)}
              </span>
            </button>
          )}
        </div>
      </header>

      <main className={`flex-1 w-full max-w-md mx-auto px-4 ${onLoginPage ? "pt-6 pb-6" : "pt-4 pb-28"}`}>
        {!onLoginPage && replayMessage && (
          <div className="mb-3 text-xs border border-slate-800 rounded bg-slate-900 px-3 py-2 text-slate-300">
            {replayMessage}
          </div>
        )}
        {children}
      </main>

      {!onLoginPage && (
        <nav
          className={`safe-bottom fixed bottom-0 left-0 right-0 bg-black/80 backdrop-blur border-t border-white/10 p-3 grid md:hidden ${
            navItems.length <= 2 ? "grid-cols-2" : navItems.length === 3 ? "grid-cols-3" : "grid-cols-4"
          }`}
        >
          {navItems.map((item) => {
            const Icon = item.icon;
            const active = currentPath === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`flex flex-col items-center gap-1 ${
                  active ? "text-red-500" : "text-slate-400 hover:text-red-500"
                }`}
              >
                <Icon size={20} />
                <span className="text-[10px] font-bold uppercase">{item.label}</span>
              </Link>
            );
          })}
        </nav>
      )}
    </div>
  );
}
