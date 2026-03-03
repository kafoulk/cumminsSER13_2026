import { Wrench } from "lucide-react";
import TriageEngine from "../components/tech/TriageEngine";

export default function Home() {
  return (
    <div className="space-y-6">
      {/* Technician Welcome Header */}
      <section className="bg-slate-900 border border-slate-800 p-6 rounded-2xl shadow-xl">
        <div className="flex items-center gap-4 mb-2">
          <Wrench className="text-red-500" size={24} />
          <h2 className="text-xl font-bold tracking-tight">Active Triage Session</h2>
        </div>
        <p className="text-slate-400 text-sm">
          Submit real field payloads to the backend orchestrator, then review the
          generated service report and canonical decision log.
        </p>
      </section>

      {/* The Guided Learning Engine */}
      <TriageEngine />
    </div>
  );
}
