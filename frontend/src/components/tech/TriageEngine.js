import { useEffect, useMemo, useRef, useState } from "react";
import { Mic } from "lucide-react";
import { SpeechRecognition } from "@capacitor-community/speech-recognition";
import {
  getDemoScenarios,
  getApiBaseUrl,
  getJobAttachments,
  getJobDetails,
  getJobTimeline,
  getWorkflow,
  replanJob,
  submitJob,
  uploadJobAttachment,
  updateWorkflowStep,
} from "../../lib/api";

const defaultForm = {
  issue_text: "Engine temp rising under load with coolant smell near radiator.",
  equipment_id: "",
  fault_code: "",
  location: "Indy Yard",
  request_supervisor_review: false,
};

function getSpeechRecognitionCtor() {
  if (typeof window === "undefined") return null;
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

function isNativeCapacitorRuntime() {
  if (typeof window === "undefined") return false;
  return Boolean(window.Capacitor?.isNativePlatform?.());
}

function hasGrantedSpeechPermission(permissionStatus) {
  const state = String(permissionStatus?.speechRecognition || "").toLowerCase();
  return state === "granted";
}

function buildSpeechErrorHint(rawError) {
  const rawText = String(
    rawError?.message || rawError?.error || rawError || "",
  ).trim();
  const lowered = rawText.toLowerCase();
  if (
    lowered.includes("not-allowed") ||
    lowered.includes("not allowed") ||
    lowered.includes("permission") ||
    lowered.includes("denied")
  ) {
    return "Microphone permission is blocked. In iPhone Settings > Cummins Service Reboot, enable Microphone and Speech Recognition.";
  }
  if (lowered.includes("not available") || lowered.includes("unavailable")) {
    return "Voice input is not available on this device. Keep typing instead.";
  }
  if (lowered.includes("not implemented")) {
    return "Native speech plugin is not synced yet. Run mobile prepare, reopen Xcode, and rebuild the app.";
  }
  return `Voice input error (${rawText || "unknown"}). You can keep typing.`;
}

function toFriendlyRisk(riskLevel) {
  const normalized = String(riskLevel || "").toUpperCase();
  if (normalized === "CRITICAL" || normalized === "HIGH")
    return "High safety risk";
  if (normalized === "MEDIUM") return "Medium risk";
  return "Low risk";
}

function toGroupedAttachments(attachments) {
  const grouped = {};
  for (const item of attachments || []) {
    const stepId = String(item?.step_id || "unassigned");
    if (!grouped[stepId]) grouped[stepId] = [];
    grouped[stepId].push(item);
  }
  return grouped;
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const value = String(reader.result || "");
      const base64Payload = value.includes(",") ? value.split(",", 2)[1] : value;
      resolve(base64Payload);
    };
    reader.onerror = () => reject(new Error("Failed to read image file."));
    reader.readAsDataURL(file);
  });
}

export default function TriageEngine() {
  const [form, setForm] = useState(defaultForm);
  const [result, setResult] = useState(null);
  const [jobDetails, setJobDetails] = useState(null);
  const [loading, setLoading] = useState(false);
  const [loadingDetails, setLoadingDetails] = useState(false);
  const [loadingWorkflow, setLoadingWorkflow] = useState(false);
  const [replanning, setReplanning] = useState(false);
  const [workflowSteps, setWorkflowSteps] = useState([]);
  const [workflowEvents, setWorkflowEvents] = useState([]);
  const [timeline, setTimeline] = useState([]);
  const [loadingTimeline, setLoadingTimeline] = useState(false);
  const [scenarioCatalog, setScenarioCatalog] = useState([]);
  const [selectedScenario, setSelectedScenario] = useState("");
  const [stepNotes, setStepNotes] = useState({});
  const [stepMeasurements, setStepMeasurements] = useState({});
  const [stepManualEscalation, setStepManualEscalation] = useState({});
  const [attachmentsByStep, setAttachmentsByStep] = useState({});
  const [attachmentCaptionByStep, setAttachmentCaptionByStep] = useState({});
  const [uploadingAttachmentStepId, setUploadingAttachmentStepId] = useState("");
  const [updatingStepId, setUpdatingStepId] = useState("");
  const [speechSupported, setSpeechSupported] = useState(false);
  const [listening, setListening] = useState(false);
  const [speechHint, setSpeechHint] = useState("");
  const [error, setError] = useState("");

  const speechRecognitionRef = useRef(null);
  const speechModeRef = useRef("none");
  const speechBaseTextRef = useRef("");
  const submitCtaRef = useRef(null);
  const createdJobId = useMemo(() => result?.job_id || "", [result]);
  const workflowMode = useMemo(() => {
    if (result?.workflow_mode) return result.workflow_mode;
    return result?.requires_approval ? "INVESTIGATION_ONLY" : "FIX_PLAN";
  }, [result]);
  const investigationOnly = workflowMode === "INVESTIGATION_ONLY";
  const statusHelpText = useMemo(() => {
    if (!result) return "";
    if (result.status === "QUEUED_OFFLINE") {
      return "Saved on this phone. It will sync when connection returns.";
    }
    if (result.requires_approval) {
      return "Supervisor review is needed before any repair steps.";
    }
    return "You can continue with the repair checklist below.";
  }, [result]);
  const maxAttachmentBytes = 3 * 1024 * 1024;

  function toAttachmentUrl(contentUrl) {
    const raw = String(contentUrl || "").trim();
    if (!raw) return "";
    if (raw.startsWith("http://") || raw.startsWith("https://")) return raw;
    return `${getApiBaseUrl()}${raw}`;
  }

  useEffect(() => {
    async function loadScenarios() {
      try {
        const data = await getDemoScenarios();
        setScenarioCatalog(data.scenarios || []);
      } catch {
        setScenarioCatalog([]);
      }
    }
    loadScenarios();
  }, []);

  useEffect(() => {
    let disposed = false;
    async function detectSpeechSupport() {
      const webSupported = Boolean(getSpeechRecognitionCtor());
      if (isNativeCapacitorRuntime()) {
        try {
          const available = await SpeechRecognition.available();
          if (!disposed) {
            setSpeechSupported(Boolean(available?.available));
          }
          return;
        } catch {
          if (!disposed) {
            setSpeechSupported(false);
          }
          return;
        }
      }
      if (!disposed) {
        setSpeechSupported(webSupported);
      }
    }
    detectSpeechSupport();
    return () => {
      disposed = true;
      if (speechRecognitionRef.current) {
        speechRecognitionRef.current.stop();
      }
      if (isNativeCapacitorRuntime()) {
        SpeechRecognition.stop().catch(() => {});
        SpeechRecognition.removeAllListeners().catch(() => {});
      }
    };
  }, []);

  async function startNativeDictation() {
    try {
      const available = await SpeechRecognition.available();
      if (!available?.available) {
        setSpeechHint(
          "Voice input is not available on this phone. Keep typing instead.",
        );
        return true;
      }
      const permission = await SpeechRecognition.checkPermissions();
      if (!hasGrantedSpeechPermission(permission)) {
        const requestedPermission =
          await SpeechRecognition.requestPermissions();
        if (!hasGrantedSpeechPermission(requestedPermission)) {
          setSpeechHint(
            "Microphone permission is blocked. In iPhone Settings > Cummins Service Reboot, enable Microphone and Speech Recognition.",
          );
          return true;
        }
      }

      speechBaseTextRef.current = String(form.issue_text || "").trim();
      await SpeechRecognition.removeAllListeners();
      await SpeechRecognition.addListener("partialResults", (data) => {
        const matches = Array.isArray(data?.matches) ? data.matches : [];
        const partialText = matches.length
          ? String(matches[matches.length - 1] || "")
          : "";
        if (!partialText) return;
        const combined = `${speechBaseTextRef.current} ${partialText}`
          .replace(/\s+/g, " ")
          .trim();
        setForm((prev) => ({ ...prev, issue_text: combined }));
      });
      await SpeechRecognition.addListener("listeningState", (data) => {
        if (data?.status === "stopped") {
          speechModeRef.current = "none";
          setListening(false);
          setSpeechHint((prev) => prev || "Voice input stopped.");
        }
      });
      await SpeechRecognition.start({
        language: "en-US",
        maxResults: 5,
        partialResults: true,
        popup: false,
      });
      speechModeRef.current = "native";
      setListening(true);
      setSpeechHint("Listening... tap Stop Voice when you are done.");
      return true;
    } catch (nativeSpeechError) {
      setSpeechHint(buildSpeechErrorHint(nativeSpeechError));
      setListening(false);
      speechModeRef.current = "none";
      await SpeechRecognition.removeAllListeners().catch(() => {});
      return true;
    }
  }

  function startWebDictation() {
    const Ctor = getSpeechRecognitionCtor();
    if (!Ctor) {
      setSpeechHint(
        "Voice input is not available on this device. Keep typing instead.",
      );
      return;
    }
    if (listening) return;

    const recognition = new Ctor();
    recognition.lang = "en-US";
    recognition.interimResults = true;
    recognition.continuous = true;
    speechBaseTextRef.current = String(form.issue_text || "").trim();

    recognition.onresult = (event) => {
      let transcript = "";
      for (let idx = 0; idx < event.results.length; idx += 1) {
        transcript += `${event.results[idx][0].transcript} `;
      }
      const combined = `${speechBaseTextRef.current} ${transcript}`
        .replace(/\s+/g, " ")
        .trim();
      setForm((prev) => ({ ...prev, issue_text: combined }));
    };
    recognition.onerror = (event) => {
      setSpeechHint(buildSpeechErrorHint(event.error));
      setListening(false);
      speechRecognitionRef.current = null;
      speechModeRef.current = "none";
    };
    recognition.onend = () => {
      setListening(false);
      speechRecognitionRef.current = null;
      speechModeRef.current = "none";
      setSpeechHint((prev) => prev || "Voice input stopped.");
    };

    try {
      recognition.start();
    } catch (startError) {
      setSpeechHint(buildSpeechErrorHint(startError));
      setListening(false);
      speechRecognitionRef.current = null;
      speechModeRef.current = "none";
      return;
    }
    speechRecognitionRef.current = recognition;
    speechModeRef.current = "web";
    setListening(true);
    setSpeechHint("Listening... tap Stop Voice when you are done.");
  }

  async function startDictation() {
    if (listening) return;
    if (isNativeCapacitorRuntime()) {
      const handled = await startNativeDictation();
      if (handled) return;
    }
    startWebDictation();
  }

  async function stopNativeDictation() {
    await SpeechRecognition.stop().catch(() => {});
    await SpeechRecognition.removeAllListeners().catch(() => {});
  }

  async function stopDictation() {
    if (speechModeRef.current === "native") {
      await stopNativeDictation();
    }
    if (speechRecognitionRef.current) {
      speechRecognitionRef.current.stop();
      speechRecognitionRef.current = null;
    }
    speechModeRef.current = "none";
    setListening(false);
    setSpeechHint("Voice input stopped.");
  }

  async function handleSubmit(event) {
    event.preventDefault();
    setLoading(true);
    setError("");
    setJobDetails(null);
    setTimeline([]);
    setAttachmentsByStep({});
    try {
      const data = await submitJob(form);
      if (data?.queued_offline && data?.local_only) {
        setResult(data);
        setWorkflowSteps(data.initial_workflow || []);
        setWorkflowEvents([]);
        setAttachmentsByStep({});
        return;
      }
      if (data?.queued_offline) {
        setResult({
          job_id: data.job_id || "queued-offline",
          status: data.status || "QUEUED_OFFLINE",
          requires_approval: true,
          workflow_mode: "INVESTIGATION_ONLY",
          workflow_intent:
            "Submission queued locally. Collect details while waiting for replay.",
          allowed_actions: [
            "capture_observation",
            "capture_measurement",
            "attach_evidence",
            "request_supervisor_review",
          ],
          suppressed_guidance: true,
          escalation_reasons: ["queued_offline_client"],
          escalation_policy_version: "client_queue_v1",
          risk_signals: {
            source: "client_queue",
            confidence: 0,
            safety_signal: false,
            warranty_signal: false,
            matched_terms: { safety: [], warranty: [] },
            rationale:
              "The request is queued on device and has not been processed by backend agents yet.",
          },
          service_report:
            "Customer complaint\n- Submission captured on device in offline queue.\n\nObservations\n- Backend agents have not run yet.\n\nDiagnostics performed\n- None yet (pending replay).\n\nManual references used\n- Pending replay.\n\nParts considered\n- Pending replay.\n\nActions taken (proposed)\n- Local queue item created.\n\nSafety/warranty notes\n- Unknown until backend triage completes.\n\nNext steps\n- Reconnect network.\n- Use the Replay Queue control.\n- Refresh this job after replay.",
          triage: {
            summary: "Pending backend replay.",
            likely_causes: [],
            next_steps: [],
            safety_flag: false,
            confidence: 0,
          },
          evidence: {
            manual_refs: [],
            parts_candidates: [],
            evidence_notes: "Pending backend replay.",
            source_chunks_used: [],
            confidence: 0,
          },
          mode_effective: "queued_local",
          model_selected: "pending_replay",
          model_tier: "pending_replay",
          schedule_hint: {
            priority_hint: "PENDING",
            eta_bucket: "Pending replay",
          },
        });
        setWorkflowSteps([]);
        setWorkflowEvents([]);
        setAttachmentsByStep({});
        return;
      }
      setResult(data);
      setWorkflowSteps(data.initial_workflow || []);
      setWorkflowEvents([]);
      setAttachmentsByStep({});
    } catch (submitError) {
      setError(submitError.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleLoadJobDetails() {
    if (!createdJobId) return;
    setLoadingDetails(true);
    setError("");
    try {
      const data = await getJobDetails(createdJobId);
      setJobDetails(data);
      if (data?.job?.final_response_json) {
        setResult((prev) => ({
          ...(prev || {}),
          ...data.job.final_response_json,
          job_id: data.job.job_id || createdJobId,
          status: data.job.status,
          requires_approval: Boolean(data.job.requires_approval),
        }));
      }
      setWorkflowSteps(data.workflow_steps || []);
      setWorkflowEvents(data.workflow_events || []);
      setAttachmentsByStep(toGroupedAttachments(data.attachments || []));
      await loadTimeline(createdJobId);
    } catch (detailsError) {
      setError(detailsError.message);
    } finally {
      setLoadingDetails(false);
    }
  }

  function updateField(field, value) {
    setForm((prev) => ({ ...prev, [field]: value }));
  }

  function loadScenarioById(scenarioId) {
    setSelectedScenario(scenarioId);
    const selected = scenarioCatalog.find((item) => item.id === scenarioId);
    if (!selected?.payload) return;
    setForm({
      issue_text:
        selected.payload.issue_text ||
        [selected.payload.symptoms || "", selected.payload.notes || ""]
          .filter(Boolean)
          .join(". "),
      equipment_id: selected.payload.equipment_id || "",
      fault_code: selected.payload.fault_code || "",
      location: selected.payload.location || "",
      request_supervisor_review: Boolean(
        selected.payload.request_supervisor_review,
      ),
    });
  }

  async function loadTimeline(jobId) {
    if (!jobId) return;
    setLoadingTimeline(true);
    try {
      const data = await getJobTimeline(jobId);
      setTimeline(data.timeline || []);
    } catch (timelineError) {
      setError(timelineError.message);
    } finally {
      setLoadingTimeline(false);
    }
  }

  async function loadAttachments(jobId) {
    if (!jobId) return;
    try {
      const data = await getJobAttachments(jobId);
      setAttachmentsByStep(toGroupedAttachments(data.attachments || []));
    } catch (attachmentError) {
      setError(attachmentError.message);
    }
  }

  async function handleAttachmentSelection(stepId, source, event) {
    const file = event?.target?.files?.[0];
    event.target.value = "";
    if (!file || !createdJobId) return;
    if (file.size > maxAttachmentBytes) {
      setError("Image too large. Max upload size is 3MB.");
      return;
    }
    setUploadingAttachmentStepId(stepId);
    setError("");
    try {
      const payload = {
        step_id: stepId,
        source,
        filename: file.name || `attachment-${Date.now()}.jpg`,
        mime_type: file.type || "image/jpeg",
        image_base64: await fileToBase64(file),
        caption: attachmentCaptionByStep[stepId] || "",
        captured_ts: new Date().toISOString(),
      };
      const response = await uploadJobAttachment(createdJobId, payload);
      if (response?.queued_offline) {
        setAttachmentsByStep((prev) => {
          const next = { ...prev };
          const queuedItem = {
            attachment_id: `queued-${Date.now()}`,
            step_id: stepId,
            filename: payload.filename,
            mime_type: payload.mime_type,
            caption: payload.caption,
            content_url: "",
            sync_state: "queued_offline",
            created_ts: new Date().toISOString(),
          };
          next[stepId] = [queuedItem, ...(next[stepId] || [])];
          return next;
        });
      } else if (response?.attachment) {
        setAttachmentsByStep((prev) => {
          const next = { ...prev };
          next[stepId] = [response.attachment, ...(next[stepId] || [])];
          return next;
        });
      } else {
        await loadAttachments(createdJobId);
      }
      setAttachmentCaptionByStep((prev) => ({ ...prev, [stepId]: "" }));
      await loadTimeline(createdJobId);
    } catch (uploadError) {
      setError(uploadError.message);
    } finally {
      setUploadingAttachmentStepId("");
    }
  }

  async function refreshWorkflow() {
    if (!createdJobId) return;
    setLoadingWorkflow(true);
    setError("");
    try {
      const data = await getWorkflow(createdJobId);
      setWorkflowSteps(data.workflow_steps || []);
      setWorkflowEvents(data.workflow_events || []);
      setResult((prev) =>
        prev
          ? {
              ...prev,
              status: data.status,
              requires_approval: data.requires_approval,
              workflow_mode: data.workflow_mode || prev.workflow_mode,
              workflow_intent: data.workflow_intent || prev.workflow_intent,
              allowed_actions: data.allowed_actions || prev.allowed_actions,
              suppressed_guidance:
                typeof data.suppressed_guidance === "boolean"
                  ? data.suppressed_guidance
                  : prev.suppressed_guidance,
              escalation_reasons: data.escalation_reasons || [],
              risk_signals: data.risk_signals || prev.risk_signals,
              escalation_policy_version:
                data.escalation_policy_version ||
                prev.escalation_policy_version,
              policy_config_hash:
                data.policy_config_hash || prev.policy_config_hash,
            }
          : prev,
      );
      await loadAttachments(createdJobId);
    } catch (workflowError) {
      setError(workflowError.message);
    } finally {
      setLoadingWorkflow(false);
    }
  }

  async function handleStepUpdate(stepId, status) {
    if (!createdJobId) return;
    setUpdatingStepId(stepId);
    setError("");
    try {
      const data = await updateWorkflowStep(createdJobId, {
        step_id: stepId,
        status,
        measurement_json: { value: stepMeasurements[stepId] || "" },
        notes: stepNotes[stepId] || "",
        actor_id: "field_technician",
        request_supervisor_review: Boolean(stepManualEscalation[stepId]),
      });
      setWorkflowSteps(data.workflow_steps || []);
      setResult((prev) =>
        prev
          ? {
              ...prev,
              status: data.status,
              requires_approval: data.requires_approval,
              workflow_mode: data.workflow_mode || prev.workflow_mode,
              workflow_intent: data.workflow_intent || prev.workflow_intent,
              allowed_actions: data.allowed_actions || prev.allowed_actions,
              suppressed_guidance:
                typeof data.suppressed_guidance === "boolean"
                  ? data.suppressed_guidance
                  : prev.suppressed_guidance,
              escalation_reasons: data.escalation_reasons || [],
              risk_signals: data.risk_signals || prev.risk_signals,
              escalation_policy_version:
                data.escalation_policy_version ||
                prev.escalation_policy_version,
              policy_config_hash:
                data.policy_config_hash || prev.policy_config_hash,
            }
          : prev,
      );
      await loadAttachments(createdJobId);
      await refreshWorkflow();
      await loadTimeline(createdJobId);
    } catch (stepError) {
      setError(stepError.message);
    } finally {
      setUpdatingStepId("");
    }
  }

  async function handleReplan() {
    if (!createdJobId) return;
    setReplanning(true);
    setError("");
    try {
      const data = await replanJob(createdJobId);
      setWorkflowSteps(data.updated_workflow || []);
      setResult((prev) =>
        prev
          ? {
              ...prev,
              status: data.status,
              requires_approval: data.requires_approval,
              workflow_mode: data.workflow_mode || prev.workflow_mode,
              workflow_intent: data.workflow_intent || prev.workflow_intent,
              allowed_actions: data.allowed_actions || prev.allowed_actions,
              suppressed_guidance:
                typeof data.suppressed_guidance === "boolean"
                  ? data.suppressed_guidance
                  : prev.suppressed_guidance,
              escalation_reasons: data.escalation_reasons || [],
              risk_signals: data.risk_signals || prev.risk_signals,
              escalation_policy_version:
                data.escalation_policy_version ||
                prev.escalation_policy_version,
              policy_config_hash:
                data.policy_config_hash || prev.policy_config_hash,
              triage: data.triage,
              evidence: data.evidence,
              schedule_hint: data.schedule_hint,
            }
          : prev,
      );
      await refreshWorkflow();
      await loadTimeline(createdJobId);
    } catch (replanError) {
      setError(replanError.message);
    } finally {
      setReplanning(false);
    }
  }

  function handleDemoToolsToggle(event) {
    const detailsElement = event.currentTarget;
    if (!detailsElement.open || typeof window === "undefined") return;

    window.requestAnimationFrame(() => {
      const quickLoadButton = detailsElement.querySelector(
        "[data-demo-safety-quick-load]",
      );
      if (!quickLoadButton) return;

      const ctaRect = submitCtaRef.current?.getBoundingClientRect();
      const visibleBottom = (ctaRect?.top ?? window.innerHeight) - 10;
      const buttonBottom = quickLoadButton.getBoundingClientRect().bottom;

      if (buttonBottom > visibleBottom) {
        window.scrollBy({
          top: buttonBottom - visibleBottom,
          behavior: "smooth",
        });
      }
    });
  }
  return (
    <div className="space-y-6">
      <form
        onSubmit={handleSubmit}
        className="relative w-full max-w-md mx-auto space-y-4 pb-[calc(2rem+env(safe-area-inset-bottom))]"
      >
        <div className="rounded-2xl border border-white/10 bg-gradient-to-b from-white/5 to-white/0 p-4 shadow-[0_12px_30px_rgba(0,0,0,0.35)]">
          <h1 className="text-lg font-bold tracking-tight">
            STEP 1: TELL US WHAT HAPPENED
          </h1>

          <p className="mt-2 text-xs text-slate-400 leading-relaxed">
            Example: &quot;Truck smells like coolant and temp climbs fast on
            hills.&quot;
          </p>

          <div className="mt-5">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-sm font-medium text-slate-200">
                Describe the issue
              </span>
            </div>

            <div className="relative">
              <textarea
                value={form.issue_text}
                onChange={(event) =>
                  updateField("issue_text", event.target.value)
                }
                className="w-full min-h-[120px] resize-none rounded-xl bg-white/5 border border-white/10 px-3 py-3 pr-12 text-[15px] font-normal text-slate-100 placeholder:text-slate-500 focus:outline-none focus:border-white/20 focus:ring-4 focus:ring-white/10"
                placeholder="Example: Engine temp rises fast under load, coolant leaking near radiator, possible safety risk."
                required
              />

              <button
                type="button"
                onClick={listening ? stopDictation : startDictation}
                className={`absolute right-3 top-3 h-9 w-9 rounded-lg border grid place-items-center active:scale-95 ${
                  listening
                    ? "border-red-500/50 bg-red-500/20 text-red-100"
                    : "border-white/10 bg-white/5 text-slate-100"
                }`}
                aria-label={listening ? "Stop voice input" : "Voice input"}
                title={listening ? "Stop voice input" : "Voice input"}
              >
                <Mic size={16} />
              </button>
            </div>
          </div>

          <div className="mt-2 flex flex-wrap gap-2 items-center">
            {!speechSupported && (
              <span className="text-xs text-slate-500">
                Voice input support depends on device/browser.
              </span>
            )}
            {speechHint && (
              <span className="text-xs text-slate-400">{speechHint}</span>
            )}
          </div>

          <div className="mt-5 border-t border-white/10 pt-4">
            <div className="text-sm font-medium text-slate-200">
              Add IDs / location <span className="text-slate-400">(optional)</span>
            </div>

            <div className="mt-4 space-y-3">
              <div>
                <div className="mb-1 text-xs font-medium text-slate-400">
                  Equipment ID (optional)
                </div>
                <input
                  value={form.equipment_id}
                  onChange={(event) =>
                    updateField("equipment_id", event.target.value)
                  }
                  className="w-full h-11 rounded-xl bg-white/5 border border-white/10 px-3 text-[15px] text-slate-100 focus:outline-none focus:border-white/20 focus:ring-4 focus:ring-white/10"
                  placeholder="EQ-1001"
                />
              </div>

              <div>
                <div className="mb-1 text-xs font-medium text-slate-400">
                  Fault Code (optional)
                </div>
                <input
                  value={form.fault_code}
                  onChange={(event) =>
                    updateField("fault_code", event.target.value)
                  }
                  className="w-full h-11 rounded-xl bg-white/5 border border-white/10 px-3 text-[15px] text-slate-100 focus:outline-none focus:border-white/20 focus:ring-4 focus:ring-white/10"
                  placeholder="P0217"
                />
              </div>

              <div>
                <div className="mb-1 text-xs font-medium text-slate-400">
                  Location (optional)
                </div>
                <input
                  value={form.location}
                  onChange={(event) =>
                    updateField("location", event.target.value)
                  }
                  className="w-full h-11 rounded-xl bg-white/5 border border-white/10 px-3 text-[15px] text-slate-100 focus:outline-none focus:border-white/20 focus:ring-4 focus:ring-white/10"
                  placeholder="Indy Yard"
                />
              </div>
            </div>

            <label className="mt-4 flex items-center gap-2 text-sm text-slate-200">
              <input
                type="checkbox"
                checked={form.request_supervisor_review}
                onChange={(event) =>
                  updateField(
                    "request_supervisor_review",
                    event.target.checked,
                  )
                }
                className="h-4 w-4 rounded border-white/20 bg-white/5"
              />
              <span>Request supervisor review</span>
            </label>
          </div>
        </div>

        <details className="pt-2" onToggle={handleDemoToolsToggle}>
          <summary className="text-xs text-slate-500 cursor-pointer">
            Demo tools
          </summary>
          <div className="pt-2 grid grid-cols-1 md:grid-cols-[1fr_auto] gap-2 items-end">
            <label className="space-y-1">
              <span className="text-xs text-slate-400">Demo scenario</span>
              <select
                value={selectedScenario}
                onChange={(event) => loadScenarioById(event.target.value)}
                className="w-full bg-black border border-slate-700 p-2 rounded text-sm"
              >
                <option value="">Select a canned scenario...</option>
                {scenarioCatalog.map((scenario) => (
                  <option key={scenario.id} value={scenario.id}>
                    {scenario.label}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              onClick={() => loadScenarioById("safety_escalation")}
              data-demo-safety-quick-load
              className="border border-orange-600 text-orange-300 hover:bg-orange-950/30 px-4 py-2 rounded font-semibold"
            >
              Quick Load Safety Scenario
            </button>
          </div>
        </details>

        <div ref={submitCtaRef} className="fixed left-4 right-4 bottom-[calc(72px+env(safe-area-inset-bottom))] max-w-md mx-auto">
          <button
            type="submit"
            disabled={loading}
            className="w-full h-12 rounded-xl bg-cummins-red text-white font-semibold shadow-lg active:scale-[0.99] disabled:opacity-60"
          >
            {loading ? "Building your checklist..." : "Get My Checklist"}
          </button>
        </div>
      </form>

      {error && (
        <div className="bg-red-900/20 border border-red-500/50 text-red-300 p-3 rounded">
          {error}
        </div>
      )}

      {result && (
        <section className="bg-slate-900 border border-slate-800 p-4 rounded-xl space-y-3">
          <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
            <h3 className="font-bold text-lg">Your Service Plan</h3>
            <div className="text-xs text-slate-300">
              Job: <span className="font-mono">{result.job_id}</span> | Status:{" "}
              <span className="font-semibold">{result.status}</span>
            </div>
          </div>
          <div className="bg-slate-800/60 border border-slate-700 p-3 rounded text-sm text-slate-200">
            {statusHelpText}
          </div>

          <pre className="whitespace-pre-wrap text-sm bg-black/40 border border-slate-800 rounded p-3">
            {result.service_report}
          </pre>

          {result.local_only && (
            <div className="bg-amber-900/20 border border-amber-600/60 text-amber-200 p-3 rounded text-sm">
              Running on on-device offline fallback model. This job is queued
              for backend reconciliation when connectivity returns.
            </div>
          )}

          {result.status === "QUEUED_OFFLINE" ? (
            <div className="bg-yellow-900/20 border border-yellow-600/60 text-yellow-200 p-3 rounded text-sm">
              This submission is queued on-device. Replay queue when
              connectivity returns to run backend agents.
            </div>
          ) : result.requires_approval ? (
            <div className="bg-orange-900/20 border border-orange-600/60 text-orange-200 p-3 rounded text-sm">
              This job escalated to supervisor review and should appear in the
              Supervisor Queue.
            </div>
          ) : (
            <div className="bg-sky-900/20 border border-sky-600/50 text-sky-200 p-3 rounded text-sm">
              This job did not escalate. Only `PENDING_APPROVAL` jobs appear in
              Supervisor Queue.
            </div>
          )}

          <div
            className={`border rounded p-3 text-sm ${
              investigationOnly
                ? "bg-amber-900/20 border-amber-600/60 text-amber-200"
                : "bg-emerald-900/20 border-emerald-600/60 text-emerald-200"
            }`}
          >
            <div className="text-xs uppercase tracking-wide mb-1">
              Workflow Mode: {workflowMode}
            </div>
            <div>
              {result.workflow_intent ||
                (investigationOnly
                  ? "Collect additional evidence for supervisor decision. Fix guidance is suppressed."
                  : "Execute repair workflow and verify issue resolution.")}
            </div>
            {Array.isArray(result.allowed_actions) &&
              result.allowed_actions.length > 0 && (
                <div className="text-xs mt-2">
                  Allowed actions: {result.allowed_actions.join(", ")}
                </div>
              )}
          </div>

          {Array.isArray(result.escalation_reasons) &&
            result.escalation_reasons.length > 0 && (
              <div className="bg-slate-800/60 border border-slate-700 text-slate-200 p-3 rounded text-sm">
                Escalation reasons: {result.escalation_reasons.join(", ")}
              </div>
            )}

          <details className="bg-black/30 border border-slate-800 rounded p-3 text-sm">
            <summary className="text-xs uppercase tracking-wide text-slate-400 cursor-pointer">
              Technical Details
            </summary>
            <div className="space-y-3 pt-3">
              <div className="bg-black/30 border border-slate-800 rounded p-3 text-sm space-y-1">
                <div className="text-xs uppercase tracking-wide text-slate-400">
                  Escalation Decision
                </div>
                <div>
                  Policy version:{" "}
                  <span className="font-mono text-slate-300">
                    {result.escalation_policy_version || "N/A"}
                  </span>
                </div>
                <div>
                  Risk source:{" "}
                  <span className="font-semibold text-slate-200">
                    {result.risk_signals?.source || "N/A"}
                  </span>
                  {" | "}confidence: {result.risk_signals?.confidence ?? "N/A"}
                </div>
                <div>
                  Safety signal:{" "}
                  {String(Boolean(result.risk_signals?.safety_signal))}
                </div>
                <div>
                  Warranty signal:{" "}
                  {String(Boolean(result.risk_signals?.warranty_signal))}
                </div>
                <div className="text-xs text-slate-400">
                  Matched safety terms:{" "}
                  {(result.risk_signals?.matched_terms?.safety || []).join(
                    ", ",
                  ) || "none"}
                </div>
                <div className="text-xs text-slate-400">
                  Matched warranty terms:{" "}
                  {(result.risk_signals?.matched_terms?.warranty || []).join(
                    ", ",
                  ) || "none"}
                </div>
                <div className="text-xs text-slate-400">
                  Rationale:{" "}
                  {result.risk_signals?.rationale || "No rationale available"}
                </div>
              </div>

              <div className="bg-black/30 border border-slate-800 rounded p-3 text-sm space-y-1">
                <div className="text-xs uppercase tracking-wide text-slate-400">
                  Model Route
                </div>
                <div>
                  Mode:{" "}
                  <span className="font-mono">
                    {result.mode_effective || "N/A"}
                  </span>
                </div>
                <div>
                  Model:{" "}
                  <span className="font-mono">
                    {result.model_selected || "N/A"}
                  </span>
                </div>
                <div>
                  Tier:{" "}
                  <span className="font-mono">
                    {result.model_tier || "N/A"}
                  </span>
                </div>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                <div className="bg-black/30 border border-slate-800 rounded p-3 text-sm">
                  <div className="text-xs uppercase text-red-400 mb-2">
                    Agent 1: Triage
                  </div>
                  <div className="text-slate-300">
                    Confidence: {result.triage?.confidence}
                  </div>
                  <div className="text-slate-300 mt-1">
                    {result.triage?.summary}
                  </div>
                </div>
                <div className="bg-black/30 border border-slate-800 rounded p-3 text-sm">
                  <div className="text-xs uppercase text-red-400 mb-2">
                    Agent 2: Parts / Evidence
                  </div>
                  <div className="text-slate-300">
                    Confidence: {result.evidence?.confidence}
                  </div>
                  <div className="text-slate-300 mt-1">
                    Parts:{" "}
                    {(result.evidence?.parts_candidates || []).join(", ")}
                  </div>
                </div>
                <div className="bg-black/30 border border-slate-800 rounded p-3 text-sm">
                  <div className="text-xs uppercase text-red-400 mb-2">
                    Agent 3: Scheduler
                  </div>
                  <div className="text-slate-300">
                    Priority: {result.schedule_hint?.priority_hint || "N/A"}
                  </div>
                  <div className="text-slate-300 mt-1">
                    ETA: {result.schedule_hint?.eta_bucket || "N/A"}
                  </div>
                </div>
              </div>
            </div>
          </details>

          <button
            onClick={handleLoadJobDetails}
            disabled={loadingDetails}
            className="border border-slate-600 hover:border-slate-500 px-3 py-2 rounded text-sm"
          >
            {loadingDetails
              ? "Loading job details..."
              : "Load Full Job + Decision Log"}
          </button>
          <button
            onClick={refreshWorkflow}
            disabled={loadingWorkflow}
            className="border border-slate-600 hover:border-slate-500 px-3 py-2 rounded text-sm ml-2"
          >
            {loadingWorkflow ? "Refreshing workflow..." : "Refresh Workflow"}
          </button>
          <button
            onClick={handleReplan}
            disabled={replanning}
            className="border border-purple-700 text-purple-200 hover:bg-purple-950/30 px-3 py-2 rounded text-sm ml-2"
          >
            {replanning ? "Replanning..." : "Replan Job"}
          </button>
          <button
            onClick={() => loadTimeline(createdJobId)}
            disabled={loadingTimeline}
            className="border border-slate-600 hover:border-slate-500 px-3 py-2 rounded text-sm ml-2"
          >
            {loadingTimeline ? "Loading timeline..." : "Load Timeline"}
          </button>
        </section>
      )}

      {workflowSteps.length > 0 && (
        <section className="bg-slate-900 border border-slate-800 p-4 rounded-xl space-y-3">
          <h3 className="font-bold text-lg">
            {investigationOnly
              ? "Step-by-Step Evidence Checklist"
              : "Step-by-Step Repair Checklist"}
          </h3>
          <p className="text-xs text-slate-400">
            {investigationOnly
              ? "Do only these checks for now. Repairs stay locked until supervisor approval."
              : "Work through each step. If a step fails, mark it and ask for help."}
          </p>
          <div className="space-y-3">
            {workflowSteps.map((step) => (
              <div
                key={step.step_id}
                className="border border-slate-800 rounded p-3 space-y-2"
              >
                <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
                  <div>
                    <div className="text-sm font-semibold">
                      {step.step_order}. {step.title}
                    </div>
                    <div className="text-xs text-slate-400">
                      {step.instructions}
                    </div>
                    <div className="text-xs text-slate-500 mt-2">
                      What to capture:{" "}
                      {(step.required_inputs || []).join(", ") || "N/A"}
                    </div>
                    <div className="text-xs text-slate-500">
                      Done when:{" "}
                      {(step.pass_criteria || []).join(", ") || "N/A"}
                    </div>
                    <div className="text-xs text-slate-500">
                      Recommended parts:{" "}
                      {investigationOnly || step.suppressed
                        ? "Suppressed pending supervisor decision."
                        : (step.recommended_parts || []).join(", ") || "N/A"}
                    </div>
                  </div>
                  <div className="text-xs font-mono">
                    {toFriendlyRisk(step.risk_level)} | status={step.status}
                  </div>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                  <input
                    value={stepMeasurements[step.step_id] || ""}
                    onChange={(event) =>
                      setStepMeasurements((prev) => ({
                        ...prev,
                        [step.step_id]: event.target.value,
                      }))
                    }
                    placeholder="Reading/number (optional)"
                    className="bg-black border border-slate-700 p-2 rounded text-sm"
                  />
                  <input
                    value={stepNotes[step.step_id] || ""}
                    onChange={(event) =>
                      setStepNotes((prev) => ({
                        ...prev,
                        [step.step_id]: event.target.value,
                      }))
                    }
                    placeholder="What you observed (optional)"
                    className="bg-black border border-slate-700 p-2 rounded text-sm"
                  />
                  <input
                    value={attachmentCaptionByStep[step.step_id] || ""}
                    onChange={(event) =>
                      setAttachmentCaptionByStep((prev) => ({
                        ...prev,
                        [step.step_id]: event.target.value,
                      }))
                    }
                    placeholder="Photo caption (optional)"
                    className="bg-black border border-slate-700 p-2 rounded text-sm"
                  />
                  <label className="flex items-center gap-2 text-xs text-slate-300 p-2">
                    <input
                      type="checkbox"
                      checked={Boolean(stepManualEscalation[step.step_id])}
                      onChange={(event) =>
                        setStepManualEscalation((prev) => ({
                          ...prev,
                          [step.step_id]: event.target.checked,
                        }))
                      }
                    />
                    Ask supervisor to review this step
                  </label>
                  <div className="flex flex-wrap gap-2 items-center p-2">
                    <label className="bg-sky-700 hover:bg-sky-600 px-3 py-1 rounded text-xs font-semibold cursor-pointer">
                      Take Photo
                      <input
                        type="file"
                        accept="image/*"
                        capture="environment"
                        className="hidden"
                        disabled={uploadingAttachmentStepId === step.step_id || !createdJobId}
                        onChange={(event) => handleAttachmentSelection(step.step_id, "camera", event)}
                      />
                    </label>
                    <label className="bg-indigo-700 hover:bg-indigo-600 px-3 py-1 rounded text-xs font-semibold cursor-pointer">
                      Upload Image
                      <input
                        type="file"
                        accept="image/*"
                        className="hidden"
                        disabled={uploadingAttachmentStepId === step.step_id || !createdJobId}
                        onChange={(event) => handleAttachmentSelection(step.step_id, "gallery", event)}
                      />
                    </label>
                    {uploadingAttachmentStepId === step.step_id && (
                      <span className="text-xs text-slate-400">Uploading image...</span>
                    )}
                  </div>
                </div>
                {(attachmentsByStep[step.step_id] || []).length > 0 && (
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                    {(attachmentsByStep[step.step_id] || []).map((item) => (
                      <div
                        key={`${item.attachment_id}-${item.created_ts || ""}`}
                        className="bg-black/40 border border-slate-800 rounded p-2 text-xs space-y-1"
                      >
                        {item.content_url ? (
                          <a
                            href={toAttachmentUrl(item.content_url)}
                            target="_blank"
                            rel="noreferrer"
                            className="block"
                          >
                            <img
                              src={toAttachmentUrl(item.content_url)}
                              alt={item.caption || item.filename || "Attachment"}
                              className="w-full h-24 object-cover rounded border border-slate-700"
                            />
                          </a>
                        ) : (
                          <div className="w-full h-24 rounded border border-slate-700 bg-slate-900 flex items-center justify-center text-slate-500">
                            queued
                          </div>
                        )}
                        <div className="truncate text-slate-300">{item.filename || "attachment"}</div>
                        <div className="truncate text-slate-500">{item.caption || "No caption"}</div>
                      </div>
                    ))}
                  </div>
                )}
                <div className="flex gap-2">
                  <button
                    disabled={updatingStepId === step.step_id}
                    onClick={() => handleStepUpdate(step.step_id, "done")}
                    className="bg-green-700 hover:bg-green-600 px-3 py-1 rounded text-xs font-semibold"
                  >
                    Mark Done
                  </button>
                  <button
                    disabled={updatingStepId === step.step_id}
                    onClick={() => handleStepUpdate(step.step_id, "blocked")}
                    className="bg-yellow-700 hover:bg-yellow-600 px-3 py-1 rounded text-xs font-semibold"
                  >
                    Need Help
                  </button>
                  <button
                    disabled={updatingStepId === step.step_id}
                    onClick={() => handleStepUpdate(step.step_id, "failed")}
                    className="bg-red-700 hover:bg-red-600 px-3 py-1 rounded text-xs font-semibold"
                  >
                    Didn't Work
                  </button>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {workflowEvents.length > 0 && (
        <details className="bg-slate-900 border border-slate-800 p-4 rounded-xl space-y-2">
          <summary className="font-bold text-lg cursor-pointer">
            Workflow Events (Advanced)
          </summary>
          <div className="max-h-64 overflow-auto border border-slate-800 rounded mt-2">
            {workflowEvents.map((event) => (
              <div
                key={event.id}
                className="p-3 border-b border-slate-800 last:border-b-0 text-xs"
              >
                <div className="font-mono text-slate-400">
                  {event.ts} | {event.actor_id} | {event.event_type}
                </div>
                <div>
                  step: {event.step_id || "N/A"} | output:{" "}
                  {JSON.stringify(event.output_json || {})}
                </div>
              </div>
            ))}
          </div>
        </details>
      )}

      {jobDetails?.decision_log?.length > 0 && (
        <details className="bg-slate-900 border border-slate-800 p-4 rounded-xl space-y-2">
          <summary className="font-bold text-lg cursor-pointer">
            Decision Log (Advanced)
          </summary>
          <div className="max-h-80 overflow-auto border border-slate-800 rounded mt-2">
            {jobDetails.decision_log.map((entry) => (
              <div
                key={entry.id}
                className="p-3 border-b border-slate-800 last:border-b-0 text-sm"
              >
                <div className="font-mono text-xs text-slate-400">
                  {entry.ts} | {entry.agent_id} | {entry.action}
                </div>
                <div className="text-slate-200">
                  confidence: {entry.confidence}
                </div>
              </div>
            ))}
          </div>
        </details>
      )}

      {timeline.length > 0 && (
        <details className="bg-slate-900 border border-slate-800 p-4 rounded-xl space-y-2">
          <summary className="font-bold text-lg cursor-pointer">
            Timeline (Advanced)
          </summary>
          <div className="max-h-80 overflow-auto border border-slate-800 rounded mt-2">
            {timeline.map((event) => (
              <div
                key={`${event.kind}-${event.event_id}-${event.ts}`}
                className="p-3 border-b border-slate-800 last:border-b-0 text-xs"
              >
                <div className="font-mono text-slate-400">
                  {event.ts} | {event.kind} | {event.actor_id} |{" "}
                  {event.event_name}
                </div>
                {event.step_id && <div>step: {event.step_id}</div>}
                {typeof event.confidence === "number" && (
                  <div>confidence: {event.confidence}</div>
                )}
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}




