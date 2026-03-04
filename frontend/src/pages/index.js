// Removed unused Wrench icon to avoid HMR issues
import TriageEngine from "../components/tech/TriageEngine";

export default function Home() {
  return (
    <div className="space-y-6">
      {/* The Guided Learning Engine */}
      <TriageEngine />
    </div>
  );
}
