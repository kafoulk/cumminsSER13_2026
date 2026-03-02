import { Users, Clock, AlertTriangle } from 'lucide-react';

export default function Supervisor() {
  const techs = [
    { id: 1, name: 'Tech A', status: 'On-site', skill: 'Advanced' },
    { id: 2, name: 'Tech B', status: 'Available', skill: 'Junior' },
  ];

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Branch Operations</h1>
      
      {/* Emergency Alert Section */}
      <div className="bg-red-900/20 border border-red-600/50 p-4 rounded-lg flex items-center gap-4">
        <AlertTriangle className="text-red-600" size={32} />
        <div>
          <h3 className="font-bold text-red-500 underline">Emergency: Hospital Backup Generator</h3>
          <p className="text-xs">Location: North Side Branch | 12.4 miles away</p>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-slate-900 p-4 rounded-xl border border-slate-800">
          <h2 className="text-sm font-bold uppercase text-slate-500 mb-4 flex items-center gap-2">
            <Users size={16} /> Technician Status
          </h2>
          {techs.map(t => (
            <div key={t.id} className="flex justify-between p-3 border-b border-slate-800 last:border-0">
              <span>{t.name} ({t.skill})</span>
              <span className={t.status === 'Available' ? 'text-green-500' : 'text-orange-500'}>{t.status}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}