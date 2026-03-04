import {
  APP_MODE_OFFLINE_1_3B,
  getApiBaseUrl as getApiBaseUrlFromSettings,
  getAppMode,
  getDefaultApiBaseUrl,
} from "./appSettings";
import { getLocalRuntimeConfig, runLocalOfflineJob } from "./localOfflineInference";
import {
  getQueueCount,
  OFFLINE_QUEUE_EVENT,
  queueRequest,
  replayQueuedRequests,
} from "./offlineQueue";

const API_BASE_URL = getDefaultApiBaseUrl();

function getResolvedApiBaseUrl() {
  return getApiBaseUrlFromSettings();
}

function createClientJobId() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return `job-${Date.now()}-${Math.floor(Math.random() * 10000)}`;
}

function parseResponseSafely(text) {
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
}

function isBrowserOffline() {
  if (typeof navigator === "undefined") return false;
  return navigator.onLine === false;
}

function isNetworkError(error) {
  if (!error) return false;
  if (error.name === "AbortError") return true;
  return error instanceof TypeError;
}

function shouldQueue(method, allowQueue) {
  return allowQueue && method !== "GET";
}

async function queueOfflineRequest(path, method, body, reason) {
  const queued = await queueRequest({ path, method, body, reason });
  return {
    queued_offline: true,
    queue_id: queued.id,
    status: "QUEUED_OFFLINE",
    detail: "Request queued locally and will replay when online.",
    path,
    job_id: body?.job_id || null,
  };
}

async function request(path, { method = "GET", body, allowQueue = true } = {}) {
  const normalizedMethod = String(method || "GET").toUpperCase();
  const baseUrl = getResolvedApiBaseUrl();

  if (shouldQueue(normalizedMethod, allowQueue) && isBrowserOffline()) {
    return queueOfflineRequest(path, normalizedMethod, body, "browser_offline");
  }

  let response;
  try {
    response = await fetch(`${baseUrl}${path}`, {
      method: normalizedMethod,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (error) {
    if (shouldQueue(normalizedMethod, allowQueue) && isNetworkError(error)) {
      return queueOfflineRequest(path, normalizedMethod, body, "network_error");
    }
    throw error;
  }

  const text = await response.text();
  const data = parseResponseSafely(text);

  if (!response.ok) {
    const detail = data?.detail || `Request failed with ${response.status}`;
    throw new Error(detail);
  }

  return data;
}

function shouldUseOfflineRouteForJob() {
  const appMode = getAppMode();
  const browserOffline = isBrowserOffline();
  return appMode === APP_MODE_OFFLINE_1_3B || browserOffline;
}

export async function submitJob(payload) {
  const normalizedPayload = {
    ...(payload || {}),
    job_id: payload?.job_id || createClientJobId(),
    is_offline: shouldUseOfflineRouteForJob(),
  };
  const response = await request("/api/job", { method: "POST", body: normalizedPayload });
  if (!response?.queued_offline) {
    return response;
  }
  return runLocalOfflineJob(normalizedPayload, {
    queue_id: response.queue_id,
    queued_at: new Date().toISOString(),
  });
}

export function getJobDetails(jobId) {
  return request(`/api/job/${jobId}`);
}

export function getJobTimeline(jobId) {
  return request(`/api/job/${jobId}/timeline`);
}

export function getSupervisorQueue() {
  return request("/api/supervisor/queue");
}

export function approveJob(payload) {
  return request("/api/supervisor/approve", { method: "POST", body: payload });
}

export function syncOfflineQueue() {
  return request("/api/sync", { method: "POST", allowQueue: false });
}

export function getWorkflow(jobId) {
  return request(`/api/job/${jobId}/workflow`);
}

export function updateWorkflowStep(jobId, payload) {
  return request(`/api/job/${jobId}/workflow/step`, {
    method: "POST",
    body: payload,
  });
}

export function replanJob(jobId) {
  return request(`/api/job/${jobId}/replan`, { method: "POST" });
}

export function getAgentMetrics(day) {
  const query = day ? `?day=${encodeURIComponent(day)}` : "";
  return request(`/api/metrics/agent-performance${query}`);
}

export function getDemoScenarios() {
  return request("/api/demo/scenarios");
}

export function getRuntimeConfig(isOffline) {
  const query = `?is_offline=${isOffline ? "true" : "false"}`;
  return request(`/api/config/runtime${query}`).catch((error) => {
    if (isNetworkError(error)) {
      return getLocalRuntimeConfig(Boolean(isOffline));
    }
    throw error;
  });
}

export function getHealth(isOffline) {
  const query = `?is_offline=${isOffline ? "true" : "false"}`;
  return request(`/api/health${query}`, { allowQueue: false }).catch((error) => {
    if (isNetworkError(error)) {
      return {
        status: "degraded_offline_local",
        ts: new Date().toISOString(),
        ...getLocalRuntimeConfig(Boolean(isOffline)),
      };
    }
    throw error;
  });
}

export async function replayOfflineQueue() {
  return replayQueuedRequests({ apiBaseUrl: getResolvedApiBaseUrl() });
}

export async function getOfflineQueueStatus() {
  return { count: await getQueueCount() };
}

export function onOfflineQueueChanged(callback) {
  if (typeof window === "undefined") return () => {};
  const handler = () => callback();
  window.addEventListener(OFFLINE_QUEUE_EVENT, handler);
  return () => window.removeEventListener(OFFLINE_QUEUE_EVENT, handler);
}

export function getApiBaseUrl() {
  return getResolvedApiBaseUrl();
}

export { API_BASE_URL };
