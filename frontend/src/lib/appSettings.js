export const APP_MODE_ONLINE_8B = "online_8b";
export const APP_MODE_OFFLINE_1_3B = "offline_1_3b";
export const APP_SETTINGS_CHANGED_EVENT = "app-settings-changed";

const API_BASE_URL_KEY = "cummins_api_base_url";

function hasWindow() {
  return typeof window !== "undefined";
}

function resolveDefaultApiBaseUrl() {
  const envDefault = process.env.NEXT_PUBLIC_API_BASE_URL || "";
  if (!hasWindow()) {
    return envDefault || "http://127.0.0.1:9054";
  }
  const capacitorRuntime = window.Capacitor;
  const isNative = Boolean(capacitorRuntime?.isNativePlatform?.());
  if (!isNative) {
    return envDefault || "http://127.0.0.1:9054";
  }
  const platform = String(capacitorRuntime?.getPlatform?.() || "");
  if (platform === "android") {
    return (
      process.env.NEXT_PUBLIC_API_BASE_URL_ANDROID ||
      envDefault ||
      "http://10.0.2.2:9054"
    );
  }
  if (platform === "ios") {
    return (
      process.env.NEXT_PUBLIC_API_BASE_URL_IOS ||
      envDefault ||
      "http://127.0.0.1:9054"
    );
  }
  return envDefault || "http://127.0.0.1:9054";
}

function emitSettingsChanged() {
  if (!hasWindow()) return;
  window.dispatchEvent(new Event(APP_SETTINGS_CHANGED_EVENT));
}

function sanitizeApiBaseUrl(value) {
  const trimmed = String(value || "").trim();
  if (!trimmed) return resolveDefaultApiBaseUrl();
  return trimmed.replace(/\/+$/, "");
}

export function getDefaultApiBaseUrl() {
  return resolveDefaultApiBaseUrl();
}

export function getApiBaseUrl() {
  const fallback = resolveDefaultApiBaseUrl();
  if (!hasWindow()) return fallback;
  const saved = window.localStorage.getItem(API_BASE_URL_KEY);
  return sanitizeApiBaseUrl(saved || fallback);
}

export function setApiBaseUrl(nextValue) {
  if (!hasWindow()) return resolveDefaultApiBaseUrl();
  const normalized = sanitizeApiBaseUrl(nextValue);
  window.localStorage.setItem(API_BASE_URL_KEY, normalized);
  emitSettingsChanged();
  return normalized;
}

export function getAppMode() {
  if (!hasWindow()) {
    return APP_MODE_ONLINE_8B;
  }
  return navigator.onLine === false ? APP_MODE_OFFLINE_1_3B : APP_MODE_ONLINE_8B;
}

export function setAppMode(nextMode) {
  const normalized = nextMode === APP_MODE_OFFLINE_1_3B ? APP_MODE_OFFLINE_1_3B : APP_MODE_ONLINE_8B;
  emitSettingsChanged();
  return normalized;
}

export function subscribeAppSettingsChanged(callback) {
  if (!hasWindow()) return () => {};
  const handler = () => callback();
  window.addEventListener(APP_SETTINGS_CHANGED_EVENT, handler);
  return () => window.removeEventListener(APP_SETTINGS_CHANGED_EVENT, handler);
}
