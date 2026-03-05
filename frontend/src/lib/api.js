import {
  APP_MODE_OFFLINE_1_3B,
  getApiBaseUrl as getApiBaseUrlFromSettings,
  getAppMode,
  getDefaultApiBaseUrl,
  getInferredApiBaseUrl,
  setApiBaseUrl as persistApiBaseUrl,
} from "./appSettings";
import { getLocalRuntimeConfig, runLocalOfflineJob } from "./localOfflineInference";
import {
  getQueueCount,
  getQueuedRequests,
  OFFLINE_QUEUE_EVENT,
  queueRequest,
  replayQueuedRequests,
} from "./offlineQueue";

const API_BASE_URL = getDefaultApiBaseUrl();

function getResolvedApiBaseUrl() {
  return getApiBaseUrlFromSettings();
}

function normalizeBaseUrl(value) {
  return String(value || "")
    .trim()
    .replace(/\/+$/, "");
}

function appendUniqueUrl(urls, candidate) {
  const normalized = normalizeBaseUrl(candidate);
  if (!normalized) return;
  if (urls.includes(normalized)) return;
  urls.push(normalized);
}

function buildApiBaseCandidates(preferredBaseUrl) {
  const candidates = [];
  appendUniqueUrl(candidates, preferredBaseUrl);
  appendUniqueUrl(candidates, getInferredApiBaseUrl());
  appendUniqueUrl(candidates, API_BASE_URL);
  appendUniqueUrl(candidates, "http://127.0.0.1:9054");
  return candidates;
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
  const preferredBaseUrl = getResolvedApiBaseUrl();
  const candidateBaseUrls = buildApiBaseCandidates(preferredBaseUrl);

  if (shouldQueue(normalizedMethod, allowQueue) && isBrowserOffline()) {
    return queueOfflineRequest(path, normalizedMethod, body, "browser_offline");
  }

  let response = null;
  let lastError = null;
  let selectedBaseUrl = normalizeBaseUrl(preferredBaseUrl);

  for (const candidate of candidateBaseUrls) {
    selectedBaseUrl = candidate;
    try {
      response = await fetch(`${candidate}${path}`, {
        method: normalizedMethod,
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      });
      lastError = null;
      break;
    } catch (error) {
      lastError = error;
      if (!isNetworkError(error)) {
        break;
      }
    }
  }

  if (!response) {
    if (shouldQueue(normalizedMethod, allowQueue) && isNetworkError(lastError)) {
      return queueOfflineRequest(path, normalizedMethod, body, "network_error");
    }
    throw lastError || new Error("Request failed");
  }

  if (selectedBaseUrl && normalizeBaseUrl(selectedBaseUrl) !== normalizeBaseUrl(preferredBaseUrl)) {
    persistApiBaseUrl(selectedBaseUrl);
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

async function getSupervisorQueueFallback() {
  const queued = await getQueuedRequests();
  const pendingByJobId = new Map();

  for (const item of queued) {
    const method = String(item?.method || "").toUpperCase();
    if (method !== "POST" || item?.path !== "/api/job") {
      continue;
    }

    const payload = item?.body || {};
    const queuedAt = item?.ts || new Date().toISOString();
    const predicted = runLocalOfflineJob(
      {
        ...payload,
        job_id: payload?.job_id || `queued-${item.id}`,
        is_offline: true,
      },
      { queue_id: item.id, queued_at: queuedAt }
    );

    if (!predicted?.requires_approval) {
      continue;
    }

    const jobId = predicted?.job_id || payload?.job_id || `queued-${item.id}`;
    pendingByJobId.set(jobId, {
      job_id: jobId,
      updated_ts: queuedAt,
      status: "PENDING_APPROVAL",
      requires_approval: 1,
      workflow_mode: predicted?.workflow_mode || "INVESTIGATION_ONLY",
      workflow_intent:
        predicted?.workflow_intent ||
        "Collect additional evidence for supervisor decision. Repair guidance suppressed.",
      escalation_reasons: predicted?.escalation_reasons || ["queued_offline_client"],
      risk_signals: predicted?.risk_signals || {},
      approval_due_ts: null,
      timed_out: 0,
      equipment_id: payload?.equipment_id || "UNKNOWN_EQUIPMENT",
      fault_code: payload?.fault_code || "UNKNOWN_FAULT",
      symptoms: payload?.symptoms || payload?.issue_text || payload?.notes || "",
      location: payload?.location || null,
      high_risk_failed_steps: 0,
      attachment_count: 0,
      latest_attachment_ts: null,
      queued_offline: true,
      queue_id: item.id,
    });
  }

  const jobs = Array.from(pendingByJobId.values()).sort((a, b) => {
    const left = Date.parse(a.updated_ts || "") || 0;
    const right = Date.parse(b.updated_ts || "") || 0;
    return right - left;
  });

  return {
    count: jobs.length,
    jobs,
    local_only: true,
    detail: "Backend unreachable. Showing on-device queued supervisor items.",
  };
}

export async function getSupervisorQueue() {
  try {
    return await request("/api/supervisor/queue", { allowQueue: false });
  } catch (error) {
    if (!isNetworkError(error)) {
      throw error;
    }
    const fallback = await getSupervisorQueueFallback();
    if (fallback.count > 0) {
      return fallback;
    }
    const baseUrl = getResolvedApiBaseUrl();
    throw new Error(
      `Load failed. Could not reach backend at ${baseUrl}. Set Settings > Backend Base URL to your laptop IP (http://<LAN-IP>:9054).`
    );
  }
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

export function uploadJobAttachment(jobId, payload) {
  return request(`/api/job/${jobId}/attachments`, {
    method: "POST",
    body: payload,
  });
}

export function getJobAttachments(jobId) {
  return request(`/api/job/${jobId}/attachments`);
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

export function getIssueHistory(params = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === null || value === undefined || value === "") return;
    query.set(key, String(value));
  });
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request(`/api/issues${suffix}`);
}

export function getSimilarIssues(jobId, limit = 5) {
  const suffix = Number.isFinite(Number(limit)) ? `?limit=${Math.max(1, Number(limit))}` : "";
  return request(`/api/issues/${jobId}/similar${suffix}`);
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
