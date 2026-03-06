import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/router";
import { ShieldCheck, Wrench } from "lucide-react";
import {
  AUTH_ROLE_SUPERVISOR,
  AUTH_ROLE_TECHNICIAN,
  canAccessPath,
  getAuthSession,
  getRoleHomePath,
  normalizeRoutePath,
  setAuthSession,
} from "../lib/authSession";

function getDefaultName(role) {
  return role === AUTH_ROLE_SUPERVISOR ? "Supervisor" : "Field Technician";
}

export default function LoginPage() {
  const router = useRouter();
  const [role, setRole] = useState(AUTH_ROLE_TECHNICIAN);
  const [displayName, setDisplayName] = useState(getDefaultName(AUTH_ROLE_TECHNICIAN));
  const [error, setError] = useState("");

  const nextPath = useMemo(() => {
    const rawNext = Array.isArray(router.query.next)
      ? router.query.next[0]
      : router.query.next;
    return normalizeRoutePath(rawNext || "");
  }, [router.query.next]);

  useEffect(() => {
    const existingSession = getAuthSession();
    if (!existingSession) return;
    router.replace(getRoleHomePath(existingSession.role));
  }, [router]);

  function handleRoleSelect(nextRole) {
    setRole(nextRole);
    setError("");
    setDisplayName((prev) => {
      const trimmed = prev.trim();
      if (!trimmed) return getDefaultName(nextRole);
      if (
        trimmed === getDefaultName(AUTH_ROLE_TECHNICIAN) ||
        trimmed === getDefaultName(AUTH_ROLE_SUPERVISOR)
      ) {
        return getDefaultName(nextRole);
      }
      return prev;
    });
  }

  function getRedirectPath(nextRole) {
    if (!nextPath || nextPath === "/login") return getRoleHomePath(nextRole);
    if (!canAccessPath(nextRole, nextPath)) return getRoleHomePath(nextRole);
    return nextPath;
  }

  function handleSubmit(event) {
    event.preventDefault();
    const normalizedName = String(displayName || "").trim();
    if (!normalizedName) {
      setError("Name is required.");
      return;
    }

    const session = setAuthSession({
      role,
      display_name: normalizedName,
      signed_in_at: new Date().toISOString(),
    });
    if (!session) {
      setError("Could not create login session.");
      return;
    }
    router.replace(getRedirectPath(session.role));
  }

  return (
    <div className="space-y-6 pt-2">
      <section className="bg-slate-900 border border-slate-800 p-5 rounded-xl space-y-4">
        <div>
          <h1 className="text-2xl font-bold">Sign In</h1>
          <p className="text-xs text-slate-400 mt-1">
            Select your role for the demo run.
          </p>
        </div>

        <div className="grid grid-cols-2 gap-2">
          <button
            type="button"
            onClick={() => handleRoleSelect(AUTH_ROLE_TECHNICIAN)}
            className={`border rounded-lg p-3 text-left transition ${
              role === AUTH_ROLE_TECHNICIAN
                ? "border-cummins-red bg-cummins-red/15 text-white"
                : "border-slate-700 hover:border-slate-500 text-slate-300"
            }`}
          >
            <Wrench size={16} className="mb-1" />
            <div className="text-sm font-semibold">Technician</div>
            <div className="text-[11px] text-slate-400 mt-1">
              Field triage + workflow updates
            </div>
          </button>
          <button
            type="button"
            onClick={() => handleRoleSelect(AUTH_ROLE_SUPERVISOR)}
            className={`border rounded-lg p-3 text-left transition ${
              role === AUTH_ROLE_SUPERVISOR
                ? "border-cummins-red bg-cummins-red/15 text-white"
                : "border-slate-700 hover:border-slate-500 text-slate-300"
            }`}
          >
            <ShieldCheck size={16} className="mb-1" />
            <div className="text-sm font-semibold">Supervisor</div>
            <div className="text-[11px] text-slate-400 mt-1">
              Approval queue + decisions
            </div>
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-3">
          <label className="space-y-1 block">
            <span className="text-xs text-slate-400">Name</span>
            <input
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              className="w-full bg-black border border-slate-700 p-2 rounded"
              placeholder={getDefaultName(role)}
            />
          </label>
          {error && (
            <div className="bg-red-900/20 border border-red-600/50 p-2 rounded text-red-200 text-sm">
              {error}
            </div>
          )}
          <button
            type="submit"
            className="w-full bg-cummins-red hover:bg-red-700 transition px-4 py-2 rounded font-semibold text-sm"
          >
            Continue as {role === AUTH_ROLE_SUPERVISOR ? "Supervisor" : "Technician"}
          </button>
        </form>
      </section>
    </div>
  );
}
