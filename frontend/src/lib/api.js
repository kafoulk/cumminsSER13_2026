const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:9054";

async function request(path, { method = "GET", body } = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });

  const text = await response.text();
  const data = text ? JSON.parse(text) : {};

  if (!response.ok) {
    const detail = data?.detail || `Request failed with ${response.status}`;
    throw new Error(detail);
  }

  return data;
}

export function submitJob(payload) {
  return request("/api/job", { method: "POST", body: payload });
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
  return request("/api/sync", { method: "POST" });
}

export function getWorkflow(jobId) {
  return request(`/api/job/${jobId}/workflow`);
}

export function updateWorkflowStep(jobId, payload) {
  return request(`/api/job/${jobId}/workflow/step`, { method: "POST", body: payload });
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

export { API_BASE_URL };
