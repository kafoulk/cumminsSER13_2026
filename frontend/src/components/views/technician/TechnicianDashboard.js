import TriageEngine from '../../components/tech/TriageEngine';

export default function TechnicianDashboard() {
  return (
    <div className="animate-in fade-in duration-500">
      <header className="mb-6">
        <h2 className="text-xl font-bold border-l-4 border-red-600 pl-4">FIELD TRIAGE</h2>
        <p className="text-slate-400 text-xs mt-1">Focus: First-Time Fix Rate (FTFR) Optimization</p>
      </header>
      
      <TriageEngine />
      
      {/* Space to add technical diagrams later */}
      <div className="mt-8 border-t border-slate-800 pt-4">
        <h3 className="text-sm font-semibold text-slate-500 uppercase">Reference Manuals</h3>
        <div className="h-32 bg-slate-900/50 rounded flex items-center justify-center border-2 border-dashed border-slate-800 mt-2">
          <p className="text-xs text-slate-600 italic">Technical diagrams will appear here based on fault code...</p>
        </div>
      </div>
    </div>
  );
}