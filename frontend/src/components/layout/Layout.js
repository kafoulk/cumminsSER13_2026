import { useEffect, useState } from "react";
import { HardDrive, LayoutDashboard, Wifi, Wrench } from "lucide-react";
import Link from "next/link";
import { API_BASE_URL } from "../../lib/api";

export default function Layout({ children }) {
  const [isOnline, setIsOnline] = useState(() => {
    if (typeof window === "undefined") return true;
    return navigator.onLine;
  });

  useEffect(() => {
    const onlineHandler = () => setIsOnline(true);
    const offlineHandler = () => setIsOnline(false);
    window.addEventListener("online", onlineHandler);
    window.addEventListener("offline", offlineHandler);
    return () => {
      window.removeEventListener("online", onlineHandler);
      window.removeEventListener("offline", offlineHandler);
    };
  }, []);

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col">
      {/* Cummins Header */}
      <header className="bg-black border-b border-slate-800 p-4 flex justify-between items-center sticky top-0 z-50">
        <div className="flex items-center gap-3">
          <div className="bg-red-600 p-1.5 rounded">
            <Wrench size={20} className="text-white" />
          </div>
          <span className="font-bold tracking-tighter text-lg">CUMMINS <span className="text-red-600">SERVICE</span></span>
        </div>

        {/* Status Badge */}
        <div className={`flex items-center gap-2 px-3 py-1 rounded-full border text-[10px] font-bold ${isOnline ? 'border-green-500/30 bg-green-500/10 text-green-400' : 'border-orange-500/30 bg-orange-500/10 text-orange-400'}`}>
          {isOnline ? <Wifi size={12} /> : <HardDrive size={12} />}
          {isOnline ? 'CLOUD SYNC' : 'OFFLINE (OLLAMA)'}
        </div>
      </header>

      <main className="flex-1 container mx-auto p-4 pb-24 md:pb-4">
        <div className="text-[11px] text-slate-500 mb-3">
          Backend: <span className="font-mono">{API_BASE_URL}</span>
        </div>
        {children}
      </main>

      {/* Mobile Bottom Nav */}
      <nav className="fixed bottom-0 left-0 right-0 bg-black border-t border-slate-800 p-3 grid grid-cols-2 md:hidden">
        <Link href="/" className="flex flex-col items-center gap-1 text-slate-400 hover:text-red-500">
          <Wrench size={20} />
          <span className="text-[10px] font-bold uppercase">Triage</span>
        </Link>
        <Link href="/supervisor" className="flex flex-col items-center gap-1 text-slate-400 hover:text-red-500">
          <LayoutDashboard size={20} />
          <span className="text-[10px] font-bold uppercase">Admin</span>
        </Link>
      </nav>
    </div>
  );
}
