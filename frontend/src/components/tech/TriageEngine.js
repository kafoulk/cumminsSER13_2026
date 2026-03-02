import { useState } from 'react';

export default function TriageEngine() {
  const [step, setStep] = useState(1);
  const [history, setHistory] = useState([]);

  // Mock logic to simulate a triage conversation based on user input
  const handleNextStep = (input) => {
    if (step === 1) {
      setHistory([...history, { type: 'user', text: input }, { type: 'ai', text: 'I see. Based on that fault code, what is the current coolant temperature, and is the fan engaging?' }]);
      setStep(2);
    } else if (step === 2) {
      setHistory([...history, { type: 'user', text: input }, { type: 'ai', text: 'Confirmed. That points to a Thermostat failure. Please check the physical housing for cracks.' }]);
      setStep(3);
    }
  };

  return (
    <div className="space-y-4">
      <div className="bg-slate-800 p-4 rounded-lg min-h-[300px] border border-gray-700">
        {history.map((msg, i) => (
          <div key={i} className={`mb-4 ${msg.type === 'ai' ? 'text-red-400' : 'text-blue-400 text-right'}`}>
            <span className="text-[10px] uppercase font-bold block">{msg.type}</span>
            <p className="text-sm">{msg.text}</p>
          </div>
        ))}
        {history.length === 0 && <p className="text-gray-500 italic">Describe the engine symptoms to begin...</p>}
      </div>

      {step < 3 && (
        <div className="flex gap-2">
          <input 
            type="text" 
            placeholder="Enter observation..."
            className="flex-1 bg-black border border-gray-700 p-3 rounded text-white"
            onKeyDown={(e) => e.key === 'Enter' && (handleNextStep(e.target.value), e.target.value = '')}
          />
          <button className="bg-cummins-red px-6 py-2 font-bold rounded">SEND</button>
        </div>
      )}
    </div>
  );
}