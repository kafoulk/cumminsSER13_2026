export const AUTH_ROLE_TECHNICIAN = "technician";
export const AUTH_ROLE_SUPERVISOR = "supervisor";
export const AUTH_SESSION_CHANGED_EVENT = "auth-session-changed";

const AUTH_SESSION_KEY = "cummins_auth_session";

function hasWindow() {
  return typeof window !== "undefined";
}

function normalizeRole(value) {
  const normalized = String(value || "").trim().toLowerCase();
  if (normalized === AUTH_ROLE_SUPERVISOR) return AUTH_ROLE_SUPERVISOR;
  if (normalized === AUTH_ROLE_TECHNICIAN) return AUTH_ROLE_TECHNICIAN;
  return "";
}

function defaultDisplayName(role) {
  if (role === AUTH_ROLE_SUPERVISOR) return "Supervisor";
  return "Field Technician";
}

function normalizeRoutePath(path) {
  const raw = String(path || "").split("#")[0].split("?")[0].trim();
  if (!raw) return "/";
  const withLeadingSlash = raw.startsWith("/") ? raw : `/${raw}`;
  if (withLeadingSlash.length > 1 && withLeadingSlash.endsWith("/")) {
    return withLeadingSlash.slice(0, -1);
  }
  return withLeadingSlash;
}

function sanitizeSession(value) {
  if (!value || typeof value !== "object") return null;
  const role = normalizeRole(value.role);
  if (!role) return null;
  const displayName = String(value.display_name || value.name || "").trim();
  return {
    role,
    display_name: displayName || defaultDisplayName(role),
    signed_in_at: String(value.signed_in_at || new Date().toISOString()),
  };
}

function emitAuthChanged() {
  if (!hasWindow()) return;
  window.dispatchEvent(new Event(AUTH_SESSION_CHANGED_EVENT));
}

export { normalizeRoutePath };

export function getRoleHomePath(role) {
  return normalizeRole(role) === AUTH_ROLE_SUPERVISOR ? "/supervisor" : "/";
}

export function canAccessPath(role, routePath) {
  const normalizedRole = normalizeRole(role);
  const path = normalizeRoutePath(routePath);
  if (path === "/login") return true;
  if (!normalizedRole) return false;
  if (path === "/" || path.startsWith("/index")) {
    return normalizedRole === AUTH_ROLE_TECHNICIAN;
  }
  if (path === "/supervisor" || path.startsWith("/supervisor/")) {
    return normalizedRole === AUTH_ROLE_SUPERVISOR;
  }
  if (path === "/repair-pool" || path.startsWith("/repair-pool/")) {
    return normalizedRole === AUTH_ROLE_TECHNICIAN;
  }
  if (path === "/customer-approval" || path.startsWith("/customer-approval/")) {
    return normalizedRole === AUTH_ROLE_TECHNICIAN;
  }
  if (path === "/parts" || path.startsWith("/parts/")) {
    return normalizedRole === AUTH_ROLE_TECHNICIAN || normalizedRole === AUTH_ROLE_SUPERVISOR;
  }
  return true;
}

export function getAuthSession() {
  if (!hasWindow()) return null;
  const raw = window.localStorage.getItem(AUTH_SESSION_KEY);
  if (!raw) return null;
  try {
    return sanitizeSession(JSON.parse(raw));
  } catch {
    return null;
  }
}

export function setAuthSession(value) {
  if (!hasWindow()) return null;
  const session = sanitizeSession(value);
  if (!session) {
    clearAuthSession();
    return null;
  }
  window.localStorage.setItem(AUTH_SESSION_KEY, JSON.stringify(session));
  emitAuthChanged();
  return session;
}

export function clearAuthSession() {
  if (!hasWindow()) return;
  window.localStorage.removeItem(AUTH_SESSION_KEY);
  emitAuthChanged();
}

export function subscribeAuthSessionChanged(callback) {
  if (!hasWindow()) return () => {};
  const handler = () => callback();
  window.addEventListener(AUTH_SESSION_CHANGED_EVENT, handler);
  return () => window.removeEventListener(AUTH_SESSION_CHANGED_EVENT, handler);
}
