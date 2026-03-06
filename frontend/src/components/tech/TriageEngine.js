import { useEffect, useMemo, useRef, useState } from "react";
import { Mic } from "lucide-react";
import { SpeechRecognition } from "@capacitor-community/speech-recognition";
import {
  claimRepairJob,
  completeRepairJob,
  draftQuoteEmail,
  generateQuote,
  getCustomerApprovalQueue,
  getDemoScenarios,
  getJobPartsUsage,
  getSimilarIssues,
  getApiBaseUrl,
  getJobAttachments,
  getJobDetails,
  getJobTimeline,
  getRepairPool,
  getWorkflowParts,
  resetDemoHistory,
  recordCustomerApproval,
  getWorkflow,
  replanJob,
  submitJob,
  uploadJobAttachment,
  usePartForStep,
  updateWorkflowStep,
} from "../../lib/api";

const defaultForm = {
  issue_text: "Engine temp rising under load with coolant smell near radiator.",
  customer_name: "",
  customer_phone: "",
  customer_email: "",
  equipment_id: "",
  fault_code: "",
  location: "Indy Yard",
  request_supervisor_review: false,
};
const ACTIVE_JOB_SNAPSHOT_KEY = "cummins_active_job_snapshot_v1";
const ISSUE_ATTACHMENT_STEP_ID = "step-context-observation";
const ISSUE_ATTACHMENT_STEP_FALLBACKS = [
  ISSUE_ATTACHMENT_STEP_ID,
  "offline-context-observation",
];

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

function toFriendlyStatus(status) {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "done") return "Done";
  if (normalized === "failed") return "Did not work";
  if (normalized === "blocked") return "Needs help";
  return "Pending";
}

function stockTone(status) {
  const normalized = String(status || "").toUpperCase();
  if (normalized === "OUT_OF_STOCK") {
    return "text-red-300 border-red-700 bg-red-900/20";
  }
  if (normalized === "LOW_STOCK") {
    return "text-amber-300 border-amber-700 bg-amber-900/20";
  }
  if (normalized === "UNKNOWN") {
    return "text-slate-300 border-slate-600 bg-slate-800/40";
  }
  return "text-emerald-300 border-emerald-700 bg-emerald-900/20";
}

function toReadableInstruction(rawInstruction) {
  const compact = String(rawInstruction || "").replace(/\s+/g, " ").trim();
  if (!compact) return "Follow this checklist step and record what you find.";
  const markerMatch = compact.match(
    /^(.*?)(?:\s(?:Record:|Capture:|Pass when:|Parts to validate|If blocked\/failed:|Repair and parts guidance).*)$/i,
  );
  const cleaned = (markerMatch?.[1] || compact).trim();
  if (!cleaned) return "Follow this checklist step and record what you find.";
  return cleaned.endsWith(".") ? cleaned : `${cleaned}.`;
}

function toFriendlyToken(token) {
  const raw = String(token || "").trim().toLowerCase();
  if (!raw) return "";
  const exactMap = {
    active_dtcs: "active fault codes",
    abs_dtcs: "brake module fault codes",
    freeze_frame: "snapshot data",
    operating_context: "when the issue happens",
    connector_notes: "connector condition notes",
    harness_notes: "wire harness notes",
    component_visual_notes: "visual condition notes",
    visual_condition_notes: "visual condition notes",
    test_procedure: "test procedure used",
    test_result: "test result",
    observed_result: "observed result",
    measurement_value: "measured value",
    engine_temp: "engine temperature",
    ambient_temp: "outside temperature",
    engine_load_pct: "engine load percent",
    cooling_pressure_psi: "cooling pressure",
    coolant_level_state: "coolant level state",
    leak_inspection_notes: "leak inspection notes",
    leak_notes: "leak notes",
    fan_engagement_state: "fan engagement state",
    fan_state: "fan state",
    airflow_obstruction_notes: "airflow obstruction notes",
    airflow_notes: "airflow notes",
    radiator_condition: "radiator condition",
    thermostat_observation: "thermostat behavior",
    pump_flow_assessment: "pump flow assessment",
    coolant_return_temp: "coolant return temperature",
    lockout_status: "lockout status",
    hazard_assessment: "hazard assessment",
    supervisor_notification: "supervisor notified status",
    supervisor_notified: "supervisor notified status",
    line_pressure: "line pressure",
    pressure_drop_test: "pressure drop test result",
    pressure_drop_result: "pressure drop test result",
    sensor_signal_check: "sensor signal check",
    sensor_signal_state: "sensor signal state",
    module_comm_status: "module communication status",
    module_comm_state: "module communication status",
    fuel_rail_pressure: "fuel rail pressure",
    load_condition: "load condition",
    throttle_response_notes: "throttle response notes",
    filter_condition: "filter condition",
    line_restriction_notes: "line restriction notes",
    flow_assessment: "flow assessment",
    injector_command_state: "injector command state",
    harness_continuity: "wire continuity",
    connector_condition: "connector condition",
    sensor_pressure: "sensor pressure",
    mechanical_gauge_pressure: "manual gauge pressure",
    mechanical_pressure: "manual gauge pressure",
    engine_state: "engine state",
    oil_level: "oil level",
    oil_grade: "oil grade",
    observation_confirmation: "observation confirmation",
    variance_notes: "difference notes",
    checkpoint_confirmation: "checkpoint confirmation",
    dispatch_confirmation: "dispatch confirmation",
    eta_commitment: "ETA commitment",
    evidence_summary: "evidence summary",
    open_questions: "open questions",
    handoff_notes: "handoff notes",
    repair_plan_summary: "repair plan summary",
    parts_confirmation: "parts confirmation",
  };
  if (exactMap[raw]) return exactMap[raw];
  const map = {
    dtcs: "fault codes",
    ecu: "computer data",
    psi: "pressure",
    pct: "percent",
    abs: "brake system module",
  };
  const parts = raw
    .split("_")
    .map((part) => map[part] || part);
  return parts.filter((part, index) => index === 0 || part !== parts[index - 1]).join(" ");
}

function toFriendlyList(values) {
  if (!Array.isArray(values)) return [];
  return values
    .map((item) => {
      const value = String(item || "").trim();
      if (!value) return "";
      if (value.includes("_")) return toFriendlyToken(value);
      return value;
    })
    .filter(Boolean);
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

function truncateText(value, maxLen = 280) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (!text) return "N/A";
  if (text.length <= maxLen) return text;
  return `${text.slice(0, maxLen - 1)}...`;
}

function loadActiveJobSnapshot() {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(ACTIVE_JOB_SNAPSHOT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    if (!parsed.result || !parsed.result.job_id) return null;
    return parsed;
  } catch {
    return null;
  }
}

function saveActiveJobSnapshot(snapshot) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(ACTIVE_JOB_SNAPSHOT_KEY, JSON.stringify(snapshot));
  } catch {
    // Ignore local storage write errors.
  }
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
  const [similarIssues, setSimilarIssues] = useState([]);
  const [loadingSimilarIssues, setLoadingSimilarIssues] = useState(false);
  const [similarPreviewLoadingJobId, setSimilarPreviewLoadingJobId] = useState("");
  const [similarPreviewExpandedJobId, setSimilarPreviewExpandedJobId] = useState("");
  const [similarPreviewByJobId, setSimilarPreviewByJobId] = useState({});
  const [historySeedBusy, setHistorySeedBusy] = useState(false);
  const [historySeedMessage, setHistorySeedMessage] = useState("");
  const [historyDemoSeeded, setHistoryDemoSeeded] = useState(false);
  const [stepNotes, setStepNotes] = useState({});
  const [stepMeasurements, setStepMeasurements] = useState({});
  const [stepManualEscalation, setStepManualEscalation] = useState({});
  const [attachmentsByStep, setAttachmentsByStep] = useState({});
  const [pendingIssuePhotos, setPendingIssuePhotos] = useState([]);
  const [issuePhotoCaption, setIssuePhotoCaption] = useState("");
  const [uploadingIssuePhotos, setUploadingIssuePhotos] = useState(false);
  const [attachmentCaptionByStep, setAttachmentCaptionByStep] = useState({});
  const [uploadingAttachmentStepId, setUploadingAttachmentStepId] = useState("");
  const [updatingStepId, setUpdatingStepId] = useState("");
  const [speechSupported, setSpeechSupported] = useState(false);
  const [listening, setListening] = useState(false);
  const [speechHint, setSpeechHint] = useState("");
  const [error, setError] = useState("");
  const [quoteBusy, setQuoteBusy] = useState(false);
  const [emailBusy, setEmailBusy] = useState(false);
  const [customerBusy, setCustomerBusy] = useState(false);
  const [customerQueue, setCustomerQueue] = useState([]);
  const [openTickets, setOpenTickets] = useState([]);
  const [loadingCustomerQueue, setLoadingCustomerQueue] = useState(false);
  const [loadingOpenTickets, setLoadingOpenTickets] = useState(false);
  const [customerQueueBusyJobId, setCustomerQueueBusyJobId] = useState("");
  const [openTicketBusyJobId, setOpenTicketBusyJobId] = useState("");
  const [queueActionMessage, setQueueActionMessage] = useState("");
  const [completeBusy, setCompleteBusy] = useState(false);
  const [completionNotes, setCompletionNotes] = useState("");
  const [workflowPartsByStep, setWorkflowPartsByStep] = useState({});
  const [workflowPartsLocation, setWorkflowPartsLocation] = useState("");
  const [workflowPartsEnabled, setWorkflowPartsEnabled] = useState(false);
  const [workflowPartsStatus, setWorkflowPartsStatus] = useState("");
  const [partsUsage, setPartsUsage] = useState([]);
  const [loadingWorkflowParts, setLoadingWorkflowParts] = useState(false);
  const [usingPartKey, setUsingPartKey] = useState("");
  const [partQtyByKey, setPartQtyByKey] = useState({});
  const [partsActionMessage, setPartsActionMessage] = useState("");
  const [workflowActionMessage, setWorkflowActionMessage] = useState("");
  const [quoteRecipientName, setQuoteRecipientName] = useState("");
  const [quoteRecipientEmail, setQuoteRecipientEmail] = useState("");
  const [activeMenu, setActiveMenu] = useState("capture");

  const speechRecognitionRef = useRef(null);
  const speechModeRef = useRef("none");
  const speechBaseTextRef = useRef("");
  const submitCtaRef = useRef(null);
  const createdJobId = useMemo(() => result?.job_id || "", [result]);
  const workflowMode = useMemo(() => {
    if (result?.workflow_mode) return result.workflow_mode;
    const status = String(result?.status || "").toUpperCase();
    if (
      [
        "DIAGNOSTIC_IN_PROGRESS",
        "PENDING_QUOTE_APPROVAL",
        "AWAITING_CUSTOMER_APPROVAL",
        "QUOTE_REWORK_REQUIRED",
      ].includes(status)
    ) {
      return "INVESTIGATION_ONLY";
    }
    return result?.requires_approval ? "INVESTIGATION_ONLY" : "FIX_PLAN";
  }, [result]);
  const investigationOnly = workflowMode === "INVESTIGATION_ONLY";
  const statusHelpText = useMemo(() => {
    if (!result) return "";
    const status = String(result.status || "").toUpperCase();
    if (status === "QUEUED_OFFLINE") {
      return "Saved on this phone. It will sync when connection returns.";
    }
    if (status === "DIAGNOSTIC_IN_PROGRESS") {
      return "Run the diagnostic checklist first, then generate quote and customer email.";
    }
    if (status === "PENDING_QUOTE_APPROVAL") {
      return "Legacy status: quote is waiting for supervisor sign-off.";
    }
    if (status === "AWAITING_CUSTOMER_APPROVAL") {
      return "Waiting for customer approval. Repair pool opens after customer approves.";
    }
    if (status === "REPAIR_POOL_OPEN") {
      return "Customer approved. This ticket is now available in Repair Pool.";
    }
    if (status === "REPAIR_COMPLETED") {
      return "Repair is completed. This ticket is archived in the supervisor ticket ledger.";
    }
    if (result.requires_approval) {
      return "Additional review is required before repair steps can continue.";
    }
    return "You can continue with the repair checklist below.";
  }, [result]);
  const quotePackage = useMemo(() => result?.quote_package || null, [result]);
  const quoteEmailDraft = useMemo(() => result?.quote_email_draft || null, [result]);
  const quoteStage = useMemo(() => String(result?.quote_stage || "").toUpperCase(), [result]);
  const canGenerateQuote = useMemo(() => {
    if (!result) return false;
    return ["DIAGNOSTIC_IN_PROGRESS", "QUOTE_REWORK_REQUIRED", "READY"].includes(String(result.status || "").toUpperCase());
  }, [result]);
  const canDraftQuoteEmail = useMemo(() => {
    if (!result || !quotePackage) return false;
    return ["DIAGNOSTIC_IN_PROGRESS", "QUOTE_REWORK_REQUIRED", "READY"].includes(String(result.status || "").toUpperCase());
  }, [result, quotePackage]);
  const canRecordCustomerDecision = useMemo(() => {
    if (!result) return false;
    return String(result.status || "").toUpperCase() === "AWAITING_CUSTOMER_APPROVAL";
  }, [result]);
  const canCompleteRepair = useMemo(() => {
    if (!result) return false;
    return ["REPAIR_POOL_OPEN", "REPAIR_IN_PROGRESS"].includes(
      String(result.status || "").toUpperCase(),
    );
  }, [result]);
  const canUsePartsNow = useMemo(() => {
    const status = String(result?.status || workflowPartsStatus || "").toUpperCase();
    const inRepair = ["REPAIR_POOL_OPEN", "REPAIR_IN_PROGRESS"].includes(status);
    return inRepair && workflowPartsEnabled;
  }, [result?.status, workflowPartsEnabled, workflowPartsStatus]);
  const issueLevelAttachments = useMemo(() => {
    const all = [];
    for (const stepId of ISSUE_ATTACHMENT_STEP_FALLBACKS) {
      all.push(...(attachmentsByStep[stepId] || []));
    }
    return all;
  }, [attachmentsByStep]);
  const customerInfo = useMemo(() => {
    const payload = jobDetails?.job?.field_payload_json || {};
    return {
      name: String(payload.customer_name || result?.customer_info?.name || form.customer_name || "").trim(),
      phone: String(payload.customer_phone || result?.customer_info?.phone || form.customer_phone || "").trim(),
      email: String(payload.customer_email || result?.customer_info?.email || form.customer_email || "").trim(),
    };
  }, [jobDetails, result, form.customer_name, form.customer_phone, form.customer_email]);
  const maxAttachmentBytes = 3 * 1024 * 1024;

  function toAttachmentUrl(contentUrl) {
    const raw = String(contentUrl || "").trim();
    if (!raw) return "";
    if (raw.startsWith("http://") || raw.startsWith("https://")) return raw;
    return `${getApiBaseUrl()}${raw}`;
  }

  async function loadWorkflowPartsForJob(jobId) {
    if (!jobId) {
      setWorkflowPartsByStep({});
      setWorkflowPartsLocation("");
      setWorkflowPartsEnabled(false);
      setWorkflowPartsStatus("");
      return;
    }
    setLoadingWorkflowParts(true);
    try {
      const data = await getWorkflowParts(jobId);
      const map = {};
      for (const item of data?.steps || []) {
        map[String(item?.step_id || "")] = Array.isArray(item?.parts) ? item.parts : [];
      }
      setWorkflowPartsByStep(map);
      setWorkflowPartsLocation(String(data?.location || ""));
      setWorkflowPartsEnabled(Boolean(data?.parts_enabled));
      setWorkflowPartsStatus(String(data?.status || ""));
    } catch {
      // Keep previous values during transient connectivity issues.
    } finally {
      setLoadingWorkflowParts(false);
    }
  }

  async function loadPartsUsageForJob(jobId) {
    if (!jobId) {
      setPartsUsage([]);
      return;
    }
    try {
      const data = await getJobPartsUsage(jobId);
      setPartsUsage(Array.isArray(data?.usage) ? data.usage : []);
    } catch {
      // Keep previous values during transient connectivity issues.
    }
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

  async function loadSimilarIssuesForJob(jobId) {
    if (!jobId) {
      setSimilarIssues([]);
      setSimilarPreviewExpandedJobId("");
      setSimilarPreviewByJobId({});
      return;
    }
    setLoadingSimilarIssues(true);
    try {
      const data = await getSimilarIssues(jobId, 5);
      setSimilarIssues(data?.similar_issues || []);
      setSimilarPreviewExpandedJobId("");
      setSimilarPreviewByJobId({});
    } catch {
      setSimilarIssues([]);
      setSimilarPreviewExpandedJobId("");
      setSimilarPreviewByJobId({});
    } finally {
      setLoadingSimilarIssues(false);
    }
  }

  async function handleResetHistorySeed() {
    setHistorySeedBusy(true);
    setError("");
    setHistorySeedMessage("");
    try {
      const data = await resetDemoHistory({ clear_server: true });
      const localCount = Number(data?.local_history_count || 0);
      setHistorySeedMessage(
        `History reset complete. Loaded ${localCount} example jobs.`,
      );
      setHistoryDemoSeeded(true);
      await loadQuickQueues(false);
      if (createdJobId) {
        await loadSimilarIssuesForJob(createdJobId);
      } else {
        setSimilarIssues([]);
      }
    } catch (seedError) {
      setError(seedError.message);
    } finally {
      setHistorySeedBusy(false);
    }
  }

  async function loadQuickQueues(showSpinner = false) {
    if (showSpinner) {
      setLoadingCustomerQueue(true);
      setLoadingOpenTickets(true);
    }
    try {
      const [customerData, openData] = await Promise.all([
        getCustomerApprovalQueue({ include_rework: true, limit: 6 }),
        getRepairPool({ include_claimed: true, limit: 6 }),
      ]);
      setCustomerQueue(customerData?.jobs || []);
      setOpenTickets(openData?.jobs || []);
    } catch (queueError) {
      if (showSpinner) {
        setError(queueError.message);
      }
    } finally {
      if (showSpinner) {
        setLoadingCustomerQueue(false);
        setLoadingOpenTickets(false);
      }
    }
  }

  useEffect(() => {
    loadQuickQueues(true);
    const interval = setInterval(() => {
      loadQuickQueues(false);
    }, 10000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    const snapshot = loadActiveJobSnapshot();
    if (!snapshot?.result?.job_id) return;

    setForm((prev) => ({ ...prev, ...(snapshot.form || {}) }));
    setResult(snapshot.result || null);
    setJobDetails(snapshot.jobDetails || null);
    setWorkflowSteps(Array.isArray(snapshot.workflowSteps) ? snapshot.workflowSteps : []);
    setWorkflowEvents(Array.isArray(snapshot.workflowEvents) ? snapshot.workflowEvents : []);
    setTimeline(Array.isArray(snapshot.timeline) ? snapshot.timeline : []);
    setSimilarIssues(Array.isArray(snapshot.similarIssues) ? snapshot.similarIssues : []);
    setStepNotes(snapshot.stepNotes || {});
    setStepMeasurements(snapshot.stepMeasurements || {});
    setAttachmentsByStep(snapshot.attachmentsByStep || {});
    setWorkflowPartsByStep(snapshot.workflowPartsByStep || {});
    setWorkflowPartsLocation(String(snapshot.workflowPartsLocation || ""));
    setWorkflowPartsEnabled(Boolean(snapshot.workflowPartsEnabled));
    setWorkflowPartsStatus(String(snapshot.workflowPartsStatus || ""));
    setPartsUsage(Array.isArray(snapshot.partsUsage) ? snapshot.partsUsage : []);
    setPartQtyByKey(snapshot.partQtyByKey || {});
    setQueueActionMessage(String(snapshot.queueActionMessage || ""));
    setPartsActionMessage(String(snapshot.partsActionMessage || ""));
    setWorkflowActionMessage(
      String(
        snapshot.workflowActionMessage ||
          "Restored your active ticket from this device.",
      ),
    );
    setActiveMenu(
      snapshot.activeMenu && snapshot.activeMenu !== "capture"
        ? snapshot.activeMenu
        : "job",
    );
  }, []);

  useEffect(() => {
    if (!result?.job_id) return;
    saveActiveJobSnapshot({
      ts: new Date().toISOString(),
      form,
      result,
      jobDetails,
      workflowSteps,
      workflowEvents,
      timeline,
      similarIssues,
      stepNotes,
      stepMeasurements,
      attachmentsByStep,
      workflowPartsByStep,
      workflowPartsLocation,
      workflowPartsEnabled,
      workflowPartsStatus,
      partsUsage,
      partQtyByKey,
      queueActionMessage,
      partsActionMessage,
      workflowActionMessage,
      activeMenu,
    });
  }, [
    form,
    result,
    jobDetails,
    workflowSteps,
    workflowEvents,
    timeline,
    similarIssues,
    stepNotes,
    stepMeasurements,
    attachmentsByStep,
    workflowPartsByStep,
    workflowPartsLocation,
    workflowPartsEnabled,
    workflowPartsStatus,
    partsUsage,
    partQtyByKey,
    queueActionMessage,
    partsActionMessage,
    workflowActionMessage,
    activeMenu,
  ]);

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
    setPartsActionMessage("");
    setWorkflowActionMessage("");
    setJobDetails(null);
    setTimeline([]);
    setAttachmentsByStep({});
    setSimilarIssues([]);
    setWorkflowPartsByStep({});
    setWorkflowPartsLocation("");
    setWorkflowPartsEnabled(false);
    setWorkflowPartsStatus("");
    setPartsUsage([]);
    setPartQtyByKey({});
    setHistorySeedMessage("");
    try {
      const data = await submitJob(form);
      if (data?.queued_offline && data?.local_only) {
        setResult(data);
        setWorkflowSteps(data.initial_workflow || []);
        setWorkflowEvents([]);
        setAttachmentsByStep({});
        setWorkflowPartsByStep({});
        setWorkflowPartsLocation("");
        setWorkflowPartsEnabled(false);
        setWorkflowPartsStatus("");
        setPartsUsage([]);
        setActiveMenu("job");
        await uploadPendingIssuePhotos(
          data.job_id || "",
          data.initial_workflow || [],
        );
        setSimilarIssues([]);
        return;
      }
      if (data?.queued_offline) {
        setResult({
          job_id: data.job_id || "queued-offline",
          status: data.status || "QUEUED_OFFLINE",
          requires_approval: false,
          workflow_mode: "INVESTIGATION_ONLY",
          workflow_intent:
            "Submission queued locally. Collect details while waiting for replay.",
          allowed_actions: [
            "capture_observation",
            "capture_measurement",
            "attach_evidence",
            "prepare_quote",
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
        setWorkflowPartsByStep({});
        setWorkflowPartsLocation("");
        setWorkflowPartsEnabled(false);
        setWorkflowPartsStatus("");
        setPartsUsage([]);
        setSimilarIssues([]);
        setActiveMenu("job");
        await uploadPendingIssuePhotos(data.job_id || "", []);
        return;
      }
      setResult(data);
      setWorkflowSteps(data.initial_workflow || []);
      setWorkflowEvents([]);
      setAttachmentsByStep({});
      setSimilarIssues(
        Array.isArray(data?.similar_issue_matches)
          ? data.similar_issue_matches
          : [],
      );
      setActiveMenu("job");
      await uploadPendingIssuePhotos(
        data.job_id || "",
        data.initial_workflow || [],
      );
      await loadWorkflowPartsForJob(data.job_id || "");
      await loadPartsUsageForJob(data.job_id || "");
      if (!Array.isArray(data?.similar_issue_matches)) {
        await loadSimilarIssuesForJob(data.job_id || "");
      }
    } catch (submitError) {
      setError(submitError.message);
    } finally {
      setLoading(false);
    }
  }

  async function loadJobDetailsById(jobId) {
    if (!jobId) return;
    setLoadingDetails(true);
    setError("");
    setPartsActionMessage("");
    try {
      const data = await getJobDetails(jobId);
      setJobDetails(data);
      if (data?.job?.final_response_json) {
        setResult((prev) => ({
          ...(prev || {}),
          ...data.job.final_response_json,
          job_id: data.job.job_id || jobId,
          status: data.job.status,
          requires_approval: Boolean(data.job.requires_approval),
        }));
      }
      setWorkflowSteps(data.workflow_steps || []);
      setWorkflowEvents(data.workflow_events || []);
      setAttachmentsByStep(toGroupedAttachments(data.attachments || []));
      setActiveMenu("job");
      await Promise.all([
        loadTimeline(jobId),
        loadSimilarIssuesForJob(jobId),
        loadWorkflowPartsForJob(jobId),
        loadPartsUsageForJob(jobId),
      ]);
    } catch (detailsError) {
      setError(detailsError.message);
    } finally {
      setLoadingDetails(false);
    }
  }

  async function handlePreviewSimilarJob(jobId) {
    if (!jobId) return;
    if (similarPreviewExpandedJobId === jobId) {
      setSimilarPreviewExpandedJobId("");
      return;
    }
    setSimilarPreviewExpandedJobId(jobId);
    const cached = similarPreviewByJobId[jobId];
    if (cached) return;

    setSimilarPreviewLoadingJobId(jobId);
    try {
      const detail = await getJobDetails(jobId);
      setSimilarPreviewByJobId((prev) => ({
        ...prev,
        [jobId]: detail,
      }));
    } catch (previewError) {
      setError(previewError.message);
      setSimilarPreviewExpandedJobId("");
    } finally {
      setSimilarPreviewLoadingJobId("");
    }
  }

  async function handleLoadJobDetails() {
    await loadJobDetailsById(createdJobId);
  }

  async function handleGenerateQuote() {
    if (!createdJobId) return;
    setQuoteBusy(true);
    setError("");
    try {
      const data = await generateQuote(createdJobId);
      setResult((prev) => ({
        ...(prev || {}),
        status: data.status || prev?.status,
        quote_package: data.quote_package || prev?.quote_package,
        quote_stage: data.quote_stage || prev?.quote_stage,
      }));
      await handleLoadJobDetails();
    } catch (quoteError) {
      setError(quoteError.message);
    } finally {
      setQuoteBusy(false);
    }
  }

  async function handleDraftQuoteEmail() {
    if (!createdJobId) return;
    setEmailBusy(true);
    setError("");
    try {
      const data = await draftQuoteEmail(createdJobId, {
        recipient_name: quoteRecipientName,
        recipient_email: quoteRecipientEmail,
      });
      setResult((prev) => ({
        ...(prev || {}),
        status: data.status || prev?.status,
        quote_stage: data.quote_stage || prev?.quote_stage,
        quote_email_draft: data.quote_email_draft || prev?.quote_email_draft,
      }));
      await handleLoadJobDetails();
    } catch (emailError) {
      setError(emailError.message);
    } finally {
      setEmailBusy(false);
    }
  }

  async function handleCustomerDecision(decision) {
    if (!createdJobId) return;
    setCustomerBusy(true);
    setError("");
    try {
      await recordCustomerApproval(createdJobId, {
        decision,
        actor_id: "field_technician",
        notes: decision === "approve" ? "Customer approved quote." : "Customer declined quote.",
      });
      await handleLoadJobDetails();
      await loadQuickQueues(false);
    } catch (decisionError) {
      setError(decisionError.message);
    } finally {
      setCustomerBusy(false);
    }
  }

  async function handleQueueCustomerDecision(jobId, decision) {
    setCustomerQueueBusyJobId(jobId);
    setQueueActionMessage("");
    setError("");
    try {
      const updated = await recordCustomerApproval(jobId, {
        decision,
        actor_id: "field_technician",
        notes:
          decision === "approve"
            ? "Customer approved from technician queue."
            : "Customer declined from technician queue.",
      });
      setQueueActionMessage(`Updated ${jobId} -> ${updated.status}`);
      await loadQuickQueues(false);
      await loadJobDetailsById(jobId);
    } catch (queueDecisionError) {
      setError(queueDecisionError.message);
    } finally {
      setCustomerQueueBusyJobId("");
    }
  }

  async function handleClaimOpenTicket(jobId) {
    setOpenTicketBusyJobId(jobId);
    setQueueActionMessage("");
    setError("");
    try {
      const updated = await claimRepairJob(jobId, {
        technician_id: "field_technician",
        technician_name: "Field Technician",
      });
      setQueueActionMessage(`Claimed ${jobId} -> ${updated.status}`);
      await loadQuickQueues(false);
      await loadJobDetailsById(jobId);
    } catch (claimError) {
      setError(claimError.message);
    } finally {
      setOpenTicketBusyJobId("");
    }
  }

  async function handleCompleteRepair() {
    if (!createdJobId) return;
    setCompleteBusy(true);
    setQueueActionMessage("");
    setError("");
    try {
      const response = await completeRepairJob(createdJobId, {
        technician_id: "field_technician",
        notes: String(completionNotes || "").trim(),
      });
      if (response?.queued_offline) {
        setQueueActionMessage("Completion queued offline. It will sync when connection returns.");
      } else {
        setQueueActionMessage(`Marked ${createdJobId} as REPAIR_COMPLETED.`);
        setCompletionNotes("");
      }
      await handleLoadJobDetails();
      await loadQuickQueues(false);
    } catch (completeError) {
      setError(completeError.message);
    } finally {
      setCompleteBusy(false);
    }
  }

  function updateField(field, value) {
    setForm((prev) => ({ ...prev, [field]: value }));
  }

  function resolveIssueAttachmentStepId(candidateSteps = []) {
    const stepIds = new Set(
      (candidateSteps || []).map((step) => String(step?.step_id || "")),
    );
    if (stepIds.has(ISSUE_ATTACHMENT_STEP_ID)) {
      return ISSUE_ATTACHMENT_STEP_ID;
    }
    if (stepIds.has("offline-context-observation")) {
      return "offline-context-observation";
    }
    const firstStepId = String(candidateSteps?.[0]?.step_id || "").trim();
    return firstStepId || ISSUE_ATTACHMENT_STEP_ID;
  }

  function clearPendingIssuePhotos(photoIds = []) {
    const idSet = new Set(photoIds);
    setPendingIssuePhotos((prev) => {
      const next = [];
      for (const item of prev) {
        if (!idSet.has(item.id)) {
          next.push(item);
          continue;
        }
        if (item.preview_url && typeof URL !== "undefined") {
          URL.revokeObjectURL(item.preview_url);
        }
      }
      return next;
    });
  }

  function handleIssuePhotoSelection(source, event) {
    const files = Array.from(event?.target?.files || []);
    event.target.value = "";
    if (files.length === 0) return;
    setError("");

    const nextItems = [];
    const rejected = [];
    for (const file of files) {
      const mimeType = String(file?.type || "").toLowerCase();
      if (!mimeType.startsWith("image/")) {
        rejected.push(`${file.name || "unknown file"} is not an image`);
        continue;
      }
      if (file.size > maxAttachmentBytes) {
        rejected.push(
          `${file.name || "image"} is larger than ${maxAttachmentBytes / (1024 * 1024)}MB`,
        );
        continue;
      }
      nextItems.push({
        id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
        source,
        file,
        filename: file.name || `issue-photo-${Date.now()}.jpg`,
        mime_type: mimeType || "image/jpeg",
        caption: String(issuePhotoCaption || "").trim(),
        captured_ts: new Date().toISOString(),
        preview_url:
          typeof URL !== "undefined" ? URL.createObjectURL(file) : "",
      });
    }

    if (rejected.length > 0) {
      setError(`Some files were skipped: ${rejected.join("; ")}`);
    }
    if (nextItems.length > 0) {
      setPendingIssuePhotos((prev) => [...prev, ...nextItems]);
      setIssuePhotoCaption("");
    }
  }

  function removePendingIssuePhoto(photoId) {
    clearPendingIssuePhotos([photoId]);
  }

  async function uploadPendingIssuePhotos(jobId, candidateSteps = []) {
    if (!jobId || pendingIssuePhotos.length === 0) return;
    const stepId = resolveIssueAttachmentStepId(candidateSteps);
    const photosToUpload = [...pendingIssuePhotos];
    const uploadedIds = [];
    setUploadingIssuePhotos(true);
    setError("");
    try {
      for (const item of photosToUpload) {
        const payload = {
          step_id: stepId,
          source: item.source,
          filename: item.filename,
          mime_type: item.mime_type,
          image_base64: await fileToBase64(item.file),
          caption: item.caption || "",
          captured_ts: item.captured_ts || new Date().toISOString(),
        };
        const response = await uploadJobAttachment(jobId, payload);
        const responseStepId =
          String(response?.step_id || response?.attachment?.step_id || stepId);
        if (response?.queued_offline) {
          setAttachmentsByStep((prev) => {
            const next = { ...prev };
            const queuedItem = {
              attachment_id: `queued-${Date.now()}-${Math.random().toString(16).slice(2)}`,
              step_id: responseStepId,
              filename: payload.filename,
              mime_type: payload.mime_type,
              caption: payload.caption,
              content_url: "",
              sync_state: "queued_offline",
              created_ts: new Date().toISOString(),
            };
            next[responseStepId] = [queuedItem, ...(next[responseStepId] || [])];
            return next;
          });
          uploadedIds.push(item.id);
          continue;
        }
        if (response?.attachment) {
          setAttachmentsByStep((prev) => {
            const next = { ...prev };
            next[responseStepId] = [
              response.attachment,
              ...(next[responseStepId] || []),
            ];
            return next;
          });
          uploadedIds.push(item.id);
          continue;
        }
        await loadAttachments(jobId);
        uploadedIds.push(item.id);
      }
      await loadTimeline(jobId);
    } catch (uploadError) {
      setError(uploadError.message);
    } finally {
      if (uploadedIds.length > 0) {
        clearPendingIssuePhotos(uploadedIds);
      }
      setUploadingIssuePhotos(false);
    }
  }

  async function loadScenarioById(scenarioId) {
    setSelectedScenario(scenarioId);
    const selected = scenarioCatalog.find((item) => item.id === scenarioId);
    if (!selected?.payload) return;
    if (selected.id?.startsWith("history_") && !historyDemoSeeded) {
      try {
        setHistorySeedBusy(true);
        const seeded = await resetDemoHistory({ clear_server: true });
        const localCount = Number(seeded?.local_history_count || 0);
        setHistorySeedMessage(
          `History seeded for demo (${localCount} example jobs).`,
        );
        setHistoryDemoSeeded(true);
        setSimilarIssues([]);
      } catch {
        // Keep scenario loading even if seed call fails.
      } finally {
        setHistorySeedBusy(false);
      }
    }
    setForm({
      issue_text:
        selected.payload.issue_text ||
        [selected.payload.symptoms || "", selected.payload.notes || ""]
          .filter(Boolean)
          .join(". "),
      customer_name: selected.payload.customer_name || "",
      customer_phone: selected.payload.customer_phone || "",
      customer_email: selected.payload.customer_email || "",
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
      await Promise.all([
        loadAttachments(createdJobId),
        loadSimilarIssuesForJob(createdJobId),
        loadWorkflowPartsForJob(createdJobId),
        loadPartsUsageForJob(createdJobId),
      ]);
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
    setWorkflowActionMessage("");
    try {
      const stepNotesText = stepNotes[stepId] || "";
      const stepMeasurementValue = stepMeasurements[stepId] || "";
      const data = await updateWorkflowStep(createdJobId, {
        step_id: stepId,
        status,
        measurement_json: { value: stepMeasurementValue },
        notes: stepNotesText,
        actor_id: "field_technician",
        request_supervisor_review: false,
      });
      if (data?.queued_offline) {
        const queuedTs = new Date().toISOString();
        setWorkflowSteps((prev) =>
          (prev || []).map((step) =>
            String(step?.step_id || "") === String(stepId)
              ? {
                  ...step,
                  status,
                  updated_ts: queuedTs,
                  sync_state: "queued_offline",
                }
              : step,
          ),
        );
        setWorkflowEvents((prev) => [
          {
            event_id: `queued-step-${Date.now()}`,
            ts: queuedTs,
            step_id: stepId,
            status,
            actor_id: "field_technician",
            notes: stepNotesText,
            measurement_json: { value: stepMeasurementValue },
            sync_state: "queued_offline",
          },
          ...(prev || []),
        ]);
        setWorkflowActionMessage(
          "Step saved offline on this phone. It will sync automatically when connection returns.",
        );
        return;
      }
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
      setWorkflowActionMessage("");
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
      await Promise.all([
        loadTimeline(createdJobId),
        loadWorkflowPartsForJob(createdJobId),
        loadPartsUsageForJob(createdJobId),
      ]);
    } catch (replanError) {
      setError(replanError.message);
    } finally {
      setReplanning(false);
    }
  }

  function partUsageKey(stepId, partId) {
    return `${String(stepId || "")}::${String(partId || "")}`;
  }

  async function handleUsePart(stepId, part) {
    if (!canUsePartsNow || !createdJobId || !stepId || !part?.part_id) return;
    const key = partUsageKey(stepId, part.part_id);
    const qtyRaw = String(partQtyByKey[key] || "1").trim();
    const quantityUsed = Math.max(1, Number.parseInt(qtyRaw, 10) || 1);
    setUsingPartKey(key);
    setError("");
    setPartsActionMessage("");
    try {
      const response = await usePartForStep({
        job_id: createdJobId,
        step_id: stepId,
        part_id: part.part_id,
        quantity_used: quantityUsed,
        actor_id: "field_technician",
        actor_role: "technician",
        notes: stepNotes[stepId] || null,
      });
      if (response?.queued_offline) {
        setPartsActionMessage("Part usage queued offline. It will sync when connectivity returns.");
      } else if (response?.ok === false && response?.blocked_out_of_stock) {
        const requestId = response?.restock_request?.request_id;
        setPartsActionMessage(
          `Out of stock for ${part.part_name}. Restock request created${requestId ? ` (${requestId})` : ""}.`,
        );
      } else {
        const remaining = Number(response?.inventory?.quantity_on_hand ?? part.quantity_on_hand ?? 0);
        setPartsActionMessage(
          `Recorded ${quantityUsed} x ${part.part_name}. Remaining at ${part.location}: ${remaining}.`,
        );
      }
      await Promise.all([
        loadWorkflowPartsForJob(createdJobId),
        loadPartsUsageForJob(createdJobId),
        loadTimeline(createdJobId),
      ]);
    } catch (useError) {
      setError(useError.message);
    } finally {
      setUsingPartKey("");
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
      <section className="bg-slate-900 border border-slate-800 p-3 rounded-xl">
        <div className="text-xs uppercase tracking-wide text-slate-500 mb-2">
          Technician Menu
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          {[
            { id: "capture", label: "New Issue" },
            { id: "job", label: "Active Job" },
            { id: "customer", label: "Customer Queue" },
            { id: "tickets", label: "Open Tickets" },
          ].map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => setActiveMenu(item.id)}
              className={`px-3 py-2 rounded text-xs font-semibold border ${
                activeMenu === item.id
                  ? "bg-cummins-red/20 border-cummins-red text-white"
                  : "bg-black/20 border-slate-700 text-slate-300 hover:border-slate-500"
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
      </section>

      {activeMenu === "capture" && (
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

          <div className="mt-5 border-t border-white/10 pt-4 space-y-3">
            <div className="text-sm font-medium text-slate-200">
              Issue photos <span className="text-slate-400">(optional)</span>
            </div>
            <div className="text-xs text-slate-400">
              Add photos with the original issue. They upload right after the
              job is created.
            </div>
            <input
              value={issuePhotoCaption}
              onChange={(event) => setIssuePhotoCaption(event.target.value)}
              placeholder="Photo caption (optional)"
              className="w-full h-11 rounded-xl bg-white/5 border border-white/10 px-3 text-[15px] text-slate-100 focus:outline-none focus:border-white/20 focus:ring-4 focus:ring-white/10"
            />
            <div className="flex flex-wrap gap-2 items-center">
              <label className="bg-sky-700 hover:bg-sky-600 px-3 py-2 rounded text-xs font-semibold cursor-pointer">
                Take Photo
                <input
                  type="file"
                  accept="image/*"
                  capture="environment"
                  multiple
                  className="hidden"
                  disabled={loading || uploadingIssuePhotos}
                  onChange={(event) =>
                    handleIssuePhotoSelection("camera", event)
                  }
                />
              </label>
              <label className="bg-indigo-700 hover:bg-indigo-600 px-3 py-2 rounded text-xs font-semibold cursor-pointer">
                Upload Image
                <input
                  type="file"
                  accept="image/*"
                  multiple
                  className="hidden"
                  disabled={loading || uploadingIssuePhotos}
                  onChange={(event) =>
                    handleIssuePhotoSelection("gallery", event)
                  }
                />
              </label>
              {uploadingIssuePhotos && (
                <span className="text-xs text-slate-400">
                  Uploading issue photos...
                </span>
              )}
            </div>

            {pendingIssuePhotos.length > 0 && (
              <div className="grid grid-cols-2 gap-2">
                {pendingIssuePhotos.map((item) => (
                  <div
                    key={item.id}
                    className="bg-black/40 border border-slate-800 rounded p-2 text-xs space-y-1"
                  >
                    {item.preview_url ? (
                      <img
                        src={item.preview_url}
                        alt={item.caption || item.filename || "Issue photo"}
                        className="w-full h-24 object-cover rounded border border-slate-700"
                      />
                    ) : (
                      <div className="w-full h-24 rounded border border-slate-700 bg-slate-900 flex items-center justify-center text-slate-500">
                        image
                      </div>
                    )}
                    <div className="truncate text-slate-300">
                      {item.filename}
                    </div>
                    <div className="truncate text-slate-500">
                      {item.caption || "No caption"}
                    </div>
                    <button
                      type="button"
                      onClick={() => removePendingIssuePhoto(item.id)}
                      className="text-red-300 hover:text-red-200 text-[11px] underline"
                    >
                      Remove
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="mt-5 border-t border-white/10 pt-4">
            <div className="text-sm font-medium text-slate-200">
              Customer + IDs / location{" "}
              <span className="text-slate-400">(optional)</span>
            </div>

            <div className="mt-4 space-y-3">
              <div>
                <div className="mb-1 text-xs font-medium text-slate-400">
                  Customer name (optional)
                </div>
                <input
                  value={form.customer_name}
                  onChange={(event) =>
                    updateField("customer_name", event.target.value)
                  }
                  className="w-full h-11 rounded-xl bg-white/5 border border-white/10 px-3 text-[15px] text-slate-100 focus:outline-none focus:border-white/20 focus:ring-4 focus:ring-white/10"
                  placeholder="Alex Johnson"
                />
              </div>

              <div>
                <div className="mb-1 text-xs font-medium text-slate-400">
                  Customer phone (optional)
                </div>
                <input
                  value={form.customer_phone}
                  onChange={(event) =>
                    updateField("customer_phone", event.target.value)
                  }
                  className="w-full h-11 rounded-xl bg-white/5 border border-white/10 px-3 text-[15px] text-slate-100 focus:outline-none focus:border-white/20 focus:ring-4 focus:ring-white/10"
                  placeholder="(555) 123-4567"
                />
              </div>

              <div>
                <div className="mb-1 text-xs font-medium text-slate-400">
                  Customer email (optional)
                </div>
                <input
                  value={form.customer_email}
                  onChange={(event) =>
                    updateField("customer_email", event.target.value)
                  }
                  className="w-full h-11 rounded-xl bg-white/5 border border-white/10 px-3 text-[15px] text-slate-100 focus:outline-none focus:border-white/20 focus:ring-4 focus:ring-white/10"
                  placeholder="customer@example.com"
                />
              </div>

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

            <div className="mt-4 text-xs text-slate-400">
              {"Diagnostic -> Quote -> Customer approval flow is active. Supervisor routing is disabled in this track."}
            </div>
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
          <div className="pt-2 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={handleResetHistorySeed}
              disabled={historySeedBusy}
              className="border border-emerald-700 text-emerald-300 hover:bg-emerald-950/30 px-3 py-2 rounded text-xs font-semibold disabled:opacity-50"
            >
              {historySeedBusy ? "Resetting history..." : "Reset + Seed History Examples"}
            </button>
            {historySeedMessage && (
              <span className="text-xs text-emerald-300">{historySeedMessage}</span>
            )}
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
      )}

      {error && (
        <div className="bg-red-900/20 border border-red-500/50 text-red-300 p-3 rounded">
          {error}
        </div>
      )}

      {(activeMenu === "customer" || activeMenu === "tickets") && (
        <section className="bg-slate-900 border border-slate-800 p-4 rounded-xl space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="font-bold text-base">
              {activeMenu === "customer" ? "Customer Queue" : "Open Tickets"}
            </h3>
            <p className="text-xs text-slate-500">
              {activeMenu === "customer"
                ? "Approve or decline customer quotes."
                : "Claim tickets that are ready for repair."}
            </p>
          </div>
          <button
            type="button"
            onClick={() => loadQuickQueues(true)}
            className="border border-slate-700 hover:border-slate-500 px-3 py-1.5 rounded text-xs"
          >
            {loadingCustomerQueue || loadingOpenTickets ? "Refreshing..." : "Refresh"}
          </button>
        </div>

        {queueActionMessage && (
          <div className="bg-green-900/20 border border-green-600/50 text-green-200 p-2 rounded text-xs">
            {queueActionMessage}
          </div>
        )}

        <div className="grid grid-cols-1 gap-3">
          {activeMenu === "customer" && (
            <div className="bg-black/30 border border-slate-800 rounded p-3 space-y-2">
            <div className="text-xs uppercase tracking-wide text-slate-400">
              Customer Approval Queue ({customerQueue.length})
            </div>
            {customerQueue.length === 0 ? (
              <div className="text-xs text-slate-500">
                No jobs waiting on customer approval.
              </div>
            ) : (
              customerQueue.map((job) => (
                <div
                  key={`cust-${job.job_id}`}
                  className="border border-slate-800 rounded p-2 space-y-1"
                >
                  <div className="text-[11px] font-mono text-slate-400">{job.job_id}</div>
                  <div className="text-xs text-slate-300">
                    {job.equipment_id || "N/A"} | {job.fault_code || "N/A"}
                  </div>
                  <div className="text-xs text-slate-400">
                    Quote:{" "}
                    {job.quote_total_usd
                      ? `$${Number(job.quote_total_usd).toFixed(2)}`
                      : "N/A"}
                  </div>
                  <div className="text-xs text-slate-400">
                    Customer: {job.customer_name || job.customer_email || "N/A"}
                  </div>
                  <div className="flex gap-1.5">
                    <button
                      type="button"
                      onClick={() => handleQueueCustomerDecision(job.job_id, "approve")}
                      disabled={customerQueueBusyJobId === job.job_id}
                      className="bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50 px-2 py-1 rounded text-[11px] font-semibold"
                    >
                      Approved
                    </button>
                    <button
                      type="button"
                      onClick={() => handleQueueCustomerDecision(job.job_id, "deny")}
                      disabled={customerQueueBusyJobId === job.job_id}
                      className="bg-red-700 hover:bg-red-600 disabled:opacity-50 px-2 py-1 rounded text-[11px] font-semibold"
                    >
                      Declined
                    </button>
                  </div>
                </div>
              ))
            )}
            </div>
          )}

          {activeMenu === "tickets" && (
            <div className="bg-black/30 border border-slate-800 rounded p-3 space-y-2">
            <div className="text-xs uppercase tracking-wide text-slate-400">
              Open Tickets Queue ({openTickets.length})
            </div>
            {openTickets.length === 0 ? (
              <div className="text-xs text-slate-500">
                No tickets in repair pool yet.
              </div>
            ) : (
              openTickets.map((job) => (
                <div
                  key={`open-${job.job_id}`}
                  className="border border-slate-800 rounded p-2 space-y-1"
                >
                  <div className="text-[11px] font-mono text-slate-400">{job.job_id}</div>
                  <div className="text-xs text-slate-300">
                    {job.equipment_id || "N/A"} | {job.fault_code || "N/A"}
                  </div>
                  <div className="text-xs text-slate-400">
                    Status: {job.status || "N/A"}
                  </div>
                  <div className="flex gap-1.5">
                    {job.status === "REPAIR_POOL_OPEN" ? (
                      <button
                        type="button"
                        onClick={() => handleClaimOpenTicket(job.job_id)}
                        disabled={openTicketBusyJobId === job.job_id}
                        className="bg-indigo-700 hover:bg-indigo-600 disabled:opacity-50 px-2 py-1 rounded text-[11px] font-semibold"
                      >
                        Claim Ticket
                      </button>
                    ) : (
                      <div className="text-[11px] text-slate-500 self-center">
                        Already claimed
                      </div>
                    )}
                    <button
                      type="button"
                      onClick={() => loadJobDetailsById(job.job_id)}
                      disabled={loadingDetails}
                      className="border border-slate-700 hover:border-slate-500 disabled:opacity-50 px-2 py-1 rounded text-[11px] font-semibold"
                    >
                      Open Checklist
                    </button>
                  </div>
                </div>
              ))
            )}
            </div>
          )}
        </div>
        </section>
      )}

      {activeMenu === "job" && !result && (
        <section className="bg-slate-900 border border-slate-800 p-4 rounded-xl space-y-3">
          <h3 className="font-bold text-lg">No Active Job Loaded</h3>
          <p className="text-sm text-slate-400">
            Start from New Issue, or open a ticket from Open Tickets.
          </p>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => setActiveMenu("capture")}
              className="bg-cummins-red/20 border border-cummins-red text-white px-3 py-2 rounded text-xs font-semibold"
            >
              Go to New Issue
            </button>
            <button
              type="button"
              onClick={() => setActiveMenu("tickets")}
              className="border border-slate-700 hover:border-slate-500 px-3 py-2 rounded text-xs font-semibold"
            >
              View Open Tickets
            </button>
          </div>
        </section>
      )}

      {activeMenu === "job" && result && (
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

          {(customerInfo.name || customerInfo.phone || customerInfo.email) && (
            <div className="bg-black/30 border border-slate-800 rounded p-3 space-y-2">
              <div className="text-xs uppercase tracking-wide text-slate-400">
                Customer Info
              </div>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-2 text-sm text-slate-200">
                <div>
                  <div className="text-[11px] text-slate-500 uppercase">Name</div>
                  <div>{customerInfo.name || "N/A"}</div>
                </div>
                <div>
                  <div className="text-[11px] text-slate-500 uppercase">Phone</div>
                  <div>{customerInfo.phone || "N/A"}</div>
                </div>
                <div>
                  <div className="text-[11px] text-slate-500 uppercase">Email</div>
                  <div>{customerInfo.email || "N/A"}</div>
                </div>
              </div>
            </div>
          )}

          {(issueLevelAttachments.length > 0 || pendingIssuePhotos.length > 0) && (
            <div className="bg-black/30 border border-slate-800 rounded p-3 space-y-2">
              <div className="text-xs uppercase tracking-wide text-slate-400">
                Issue Photos
              </div>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                {issueLevelAttachments.map((item) => (
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
                          alt={item.caption || item.filename || "Issue photo"}
                          className="w-full h-24 object-cover rounded border border-slate-700"
                        />
                      </a>
                    ) : (
                      <div className="w-full h-24 rounded border border-slate-700 bg-slate-900 flex items-center justify-center text-slate-500">
                        queued
                      </div>
                    )}
                    <div className="truncate text-slate-300">
                      {item.filename || "attachment"}
                    </div>
                    <div className="truncate text-slate-500">
                      {item.caption || "No caption"}
                    </div>
                  </div>
                ))}
                {pendingIssuePhotos.map((item) => (
                  <div
                    key={`pending-${item.id}`}
                    className="bg-black/40 border border-dashed border-slate-700 rounded p-2 text-xs space-y-1"
                  >
                    {item.preview_url ? (
                      <img
                        src={item.preview_url}
                        alt={item.caption || item.filename || "Pending issue photo"}
                        className="w-full h-24 object-cover rounded border border-slate-700"
                      />
                    ) : (
                      <div className="w-full h-24 rounded border border-slate-700 bg-slate-900 flex items-center justify-center text-slate-500">
                        pending
                      </div>
                    )}
                    <div className="truncate text-slate-300">
                      {item.filename}
                    </div>
                    <div className="truncate text-slate-500">
                      {item.caption || "No caption"}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          <pre className="whitespace-pre-wrap text-sm bg-black/40 border border-slate-800 rounded p-3">
            {result.service_report}
          </pre>

          {result.local_only && (
            <div className="bg-amber-900/20 border border-amber-600/60 text-amber-200 p-3 rounded text-sm">
              You are working offline on this phone. Everything is saved here
              and will sync when connection returns.
            </div>
          )}

          {result.status === "QUEUED_OFFLINE" ? (
            <div className="bg-yellow-900/20 border border-yellow-600/60 text-yellow-200 p-3 rounded text-sm">
              This job is saved offline. It will process automatically once
              you reconnect.
            </div>
          ) : (
            <div className="bg-sky-900/20 border border-sky-600/50 text-sky-200 p-3 rounded text-sm">
              {"Flow: Gather details -> Send quote -> Get customer approval -> Start repair steps."}
            </div>
          )}

          <div className="bg-black/30 border border-slate-800 rounded p-3 space-y-3">
            <div className="text-xs uppercase tracking-wide text-slate-400">
              Quote and Customer
            </div>
            <div className="text-xs text-slate-400">
              Current stage:{" "}
              <span className="font-mono text-slate-200">
                {quoteStage ? quoteStage.replace(/_/g, " ") : "Not started"}
              </span>
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                onClick={handleGenerateQuote}
                disabled={!canGenerateQuote || quoteBusy}
                className={`px-3 py-2 rounded text-xs font-semibold ${
                  canGenerateQuote
                    ? "bg-slate-800 border border-slate-700 hover:border-slate-500"
                    : "bg-slate-900 border border-slate-800 text-slate-500"
                }`}
              >
                {quoteBusy ? "Generating..." : "1) Generate Quote"}
              </button>
              <button
                onClick={handleDraftQuoteEmail}
                disabled={!canDraftQuoteEmail || emailBusy}
                className={`px-3 py-2 rounded text-xs font-semibold ${
                  canDraftQuoteEmail
                    ? "bg-indigo-900/40 border border-indigo-700 hover:border-indigo-500"
                    : "bg-slate-900 border border-slate-800 text-slate-500"
                }`}
              >
                {emailBusy ? "Drafting..." : "2) Draft Customer Email"}
              </button>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
              <input
                value={quoteRecipientName}
                onChange={(event) => setQuoteRecipientName(event.target.value)}
                className="bg-black border border-slate-700 p-2 rounded text-sm"
                placeholder="Customer name (optional)"
              />
              <input
                value={quoteRecipientEmail}
                onChange={(event) => setQuoteRecipientEmail(event.target.value)}
                className="bg-black border border-slate-700 p-2 rounded text-sm"
                placeholder="Customer email (optional)"
              />
            </div>

            {quotePackage && (
              <div className="bg-black/40 border border-slate-800 rounded p-3 text-sm space-y-1">
                <div>
                  Quote <span className="font-mono">{quotePackage.quote_id || "N/A"}</span>
                </div>
                <div>Subtotal: ${Number(quotePackage.subtotal_usd || 0).toFixed(2)}</div>
                <div>Tax: ${Number(quotePackage.tax_usd || 0).toFixed(2)}</div>
                <div className="font-semibold">Total: ${Number(quotePackage.total_usd || 0).toFixed(2)}</div>
              </div>
            )}

            {quoteEmailDraft?.subject && (
              <details className="bg-black/40 border border-slate-800 rounded p-3 text-sm">
                <summary className="cursor-pointer text-slate-200">Email Draft Preview</summary>
                <div className="pt-3 space-y-2">
                  <div>
                    <span className="text-slate-400">Subject:</span> {quoteEmailDraft.subject}
                  </div>
                  <pre className="whitespace-pre-wrap text-xs bg-black/40 border border-slate-800 rounded p-2">
                    {quoteEmailDraft.body_text}
                  </pre>
                </div>
              </details>
            )}

            {canRecordCustomerDecision && (
              <div className="bg-emerald-900/20 border border-emerald-700/60 rounded p-3 space-y-2">
                <div className="text-sm text-emerald-200">
                  Email is ready. Record customer decision:
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() => handleCustomerDecision("approve")}
                    disabled={customerBusy}
                    className="bg-emerald-700 hover:bg-emerald-600 px-3 py-2 rounded text-xs font-semibold"
                  >
                    {customerBusy ? "Saving..." : "Customer Approved"}
                  </button>
                  <button
                    onClick={() => handleCustomerDecision("deny")}
                    disabled={customerBusy}
                    className="bg-red-700 hover:bg-red-600 px-3 py-2 rounded text-xs font-semibold"
                  >
                    {customerBusy ? "Saving..." : "Customer Declined"}
                  </button>
                </div>
              </div>
            )}
          </div>

          <div
            className={`border rounded p-3 text-sm ${
              investigationOnly
                ? "bg-amber-900/20 border-amber-600/60 text-amber-200"
                : "bg-emerald-900/20 border-emerald-600/60 text-emerald-200"
            }`}
          >
            {result.workflow_intent ||
              (investigationOnly
                ? "Run the diagnostic checklist first, then prepare the quote and customer handoff."
                : "Run the repair checklist and confirm the fix before closing the job.")}
          </div>

          {canCompleteRepair && (
            <div className="bg-emerald-900/20 border border-emerald-700/60 rounded p-3 space-y-2">
              <div className="text-sm font-semibold text-emerald-200">
                Complete Job
              </div>
              <div className="text-xs text-emerald-300">
                When repair is finished, complete the ticket to remove it from Open Tickets.
              </div>
              <input
                value={completionNotes}
                onChange={(event) => setCompletionNotes(event.target.value)}
                className="w-full bg-black border border-slate-700 p-2 rounded text-sm"
                placeholder="Completion notes (optional)"
              />
              <button
                type="button"
                onClick={handleCompleteRepair}
                disabled={completeBusy}
                className="bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50 px-3 py-2 rounded text-xs font-semibold"
              >
                {completeBusy ? "Completing..." : "Complete Job"}
              </button>
            </div>
          )}

          {String(result.status || "").toUpperCase() === "REPAIR_COMPLETED" && (
            <div className="bg-emerald-900/20 border border-emerald-700/60 rounded p-3 text-sm text-emerald-200">
              This ticket is completed and moved out of the Open Tickets queue.
            </div>
          )}

          <section className="bg-black/30 border border-slate-800 rounded p-3 space-y-2">
            <div className="flex items-center justify-between">
              <div className="text-xs uppercase tracking-wide text-slate-400">
                Similar Past Jobs
              </div>
              {loadingSimilarIssues && (
                <div className="text-[11px] text-slate-500">Checking history...</div>
              )}
            </div>
            {similarIssues.length === 0 ? (
              <div className="text-xs text-slate-500">
                No strong historical match found yet for this issue.
              </div>
            ) : (
              <div className="space-y-2">
                {similarIssues.map((item) => (
                  <div
                    key={`similar-${item.job_id}`}
                    className="border border-slate-800 rounded p-2 bg-black/20"
                  >
                    <div className="text-[11px] font-mono text-slate-400">{item.job_id}</div>
                    <div className="text-xs text-slate-300">
                      Similarity: {Math.round(Number(item.score || 0) * 100)}%
                      {" | "}
                      {item.equipment_id || "N/A"} | {item.fault_code || "N/A"}
                    </div>
                    <div className="text-xs text-slate-400 mt-1">
                      {item.issue_text || "No issue summary available."}
                    </div>
                    <div className="mt-2 flex flex-wrap gap-2">
                      <button
                        type="button"
                        onClick={() => handlePreviewSimilarJob(item.job_id)}
                        disabled={similarPreviewLoadingJobId === item.job_id}
                        className="border border-slate-700 hover:border-slate-500 px-2 py-1 rounded text-[11px] font-semibold"
                      >
                        {similarPreviewExpandedJobId === item.job_id
                          ? "Hide Details"
                          : similarPreviewLoadingJobId === item.job_id
                            ? "Loading..."
                            : "Preview"}
                      </button>
                      <button
                        type="button"
                        onClick={() => loadJobDetailsById(item.job_id)}
                        className="border border-cummins-red/60 text-cummins-red hover:bg-cummins-red/10 px-2 py-1 rounded text-[11px] font-semibold"
                      >
                        Open Job
                      </button>
                    </div>
                    {similarPreviewExpandedJobId === item.job_id &&
                      (() => {
                        const preview = similarPreviewByJobId[item.job_id];
                        const job = preview?.job || {};
                        const payload = job?.field_payload_json || {};
                        const final = job?.final_response_json || {};
                        return (
                          <div className="mt-2 border border-slate-800 rounded p-2 bg-black/30 space-y-1">
                            <div className="text-[11px] text-slate-400">
                              Status: <span className="text-slate-200">{job?.status || item.status || "N/A"}</span>
                            </div>
                            <div className="text-[11px] text-slate-400">
                              Location: <span className="text-slate-200">{payload?.location || "N/A"}</span>
                              {" | "}
                              Updated: <span className="text-slate-200">{job?.updated_ts || item.updated_ts || "N/A"}</span>
                            </div>
                            <div className="text-[11px] text-slate-400">
                              Symptoms: <span className="text-slate-200">{truncateText(payload?.symptoms, 140)}</span>
                            </div>
                            <div className="text-[11px] text-slate-400">
                              Notes: <span className="text-slate-200">{truncateText(payload?.notes, 140)}</span>
                            </div>
                            <div className="text-[11px] text-slate-400">
                              Service report:{" "}
                              <span className="text-slate-200">{truncateText(final?.service_report, 220)}</span>
                            </div>
                            <div className="text-[11px] text-slate-500">
                              Checklist steps: {Array.isArray(preview?.workflow_steps) ? preview.workflow_steps.length : 0}
                              {" | "}
                              Attachments: {Array.isArray(preview?.attachments) ? preview.attachments.length : 0}
                            </div>
                          </div>
                        );
                      })()}
                  </div>
                ))}
              </div>
            )}
          </section>

        </section>
      )}

      {activeMenu === "job" && workflowSteps.length > 0 && (
        <section className="bg-slate-900 border border-slate-800 p-4 rounded-xl space-y-3">
          <h3 className="font-bold text-lg">
            {investigationOnly
              ? "Step-by-Step Diagnostic Checklist"
              : "Step-by-Step Repair Checklist"}
          </h3>
          <p className="text-xs text-slate-400">
            {investigationOnly
              ? "Do these diagnostic checks first. Repair steps unlock after customer approval."
              : "Work through each step. If a step fails, mark it and ask for help."}
          </p>
          {workflowPartsLocation && (
            <p className="text-xs text-slate-500">
              Parts location: {workflowPartsLocation}
              {loadingWorkflowParts ? " (refreshing...)" : ""}
            </p>
          )}
          {!canUsePartsNow && (
            <p className="text-xs text-slate-500">
              Parts usage unlocks only after customer approval when repair is active.
            </p>
          )}
          {partsActionMessage && (
            <div className="bg-emerald-900/20 border border-emerald-700/60 text-emerald-200 p-2 rounded text-xs">
              {partsActionMessage}
            </div>
          )}
          {workflowActionMessage && (
            <div className="bg-sky-900/20 border border-sky-700/60 text-sky-200 p-2 rounded text-xs">
              {workflowActionMessage}
            </div>
          )}
          <div className="space-y-3">
            {workflowSteps.map((step) => {
              const stepParts = workflowPartsByStep[step.step_id] || [];
              return (
                <div
                  key={step.step_id}
                  className="border border-slate-800 rounded p-3 space-y-2"
                >
                  <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
                    <div>
                      <div className="text-sm font-semibold">
                        {step.step_order}. {step.title}
                      </div>
                      <div className="text-sm leading-relaxed text-slate-300 mt-1">
                        {toReadableInstruction(step.instructions)}
                      </div>
                      <div className="text-xs text-slate-400 mt-2">
                        <span className="text-slate-500 uppercase text-[11px]">
                          Capture
                        </span>
                        {": "}
                        {toFriendlyList(step.required_inputs || []).join(" • ") ||
                          "N/A"}
                      </div>
                      <div className="text-xs text-slate-400">
                        <span className="text-slate-500 uppercase text-[11px]">
                          Done when
                        </span>
                        {": "}
                        {toFriendlyList(step.pass_criteria || []).join(", ") ||
                          "N/A"}
                      </div>
                      <div className="text-xs text-slate-400">
                        <span className="text-slate-500 uppercase text-[11px]">
                          Recommended parts
                        </span>
                        {": "}
                        {!canUsePartsNow || investigationOnly || step.suppressed
                          ? "Shown once repair starts after customer approval."
                          : (step.recommended_parts || []).join(", ") || "N/A"}
                      </div>
                    </div>
                    <div className="flex gap-2 text-[11px] font-semibold">
                      <span className="px-2 py-1 rounded bg-red-900/20 border border-red-800 text-red-200">
                        {toFriendlyRisk(step.risk_level)}
                      </span>
                      <span className="px-2 py-1 rounded bg-slate-800 border border-slate-700 text-slate-200">
                        {toFriendlyStatus(step.status)}
                      </span>
                    </div>
                  </div>

                  {canUsePartsNow && stepParts.length > 0 && (
                    <div className="bg-black/30 border border-slate-800 rounded p-2 space-y-2">
                      <div className="text-xs uppercase tracking-wide text-slate-400">
                        Use Parts For This Step
                      </div>
                      {stepParts.map((part) => {
                        const partId = String(part?.part_id || "");
                        const key = partUsageKey(step.step_id, partId || part?.part_name);
                        const qtyValue = String(partQtyByKey[key] || "1");
                        const qtyOnHand = Number(part?.quantity_on_hand || 0);
                        const canUse = canUsePartsNow && Boolean(createdJobId) && Boolean(partId) && qtyOnHand > 0;
                        return (
                          <div
                            key={`${step.step_id}-${partId || part?.part_name || "unknown"}`}
                            className="flex flex-col md:flex-row md:items-center gap-2 border border-slate-800 rounded p-2"
                          >
                            <div className="flex-1">
                              <div className="text-xs font-semibold text-slate-200">
                                {part?.part_name || "Unknown part"}
                              </div>
                              <div className="flex flex-wrap items-center gap-2 text-[11px]">
                                <span className={`px-2 py-0.5 rounded border ${stockTone(part?.stock_status)}`}>
                                  {part?.stock_status || "UNKNOWN"}
                                </span>
                                <span className="text-slate-400">
                                  Qty: {qtyOnHand} | Part ID: {partId || "not in catalog"}
                                </span>
                              </div>
                            </div>
                            <div className="flex items-center gap-2">
                              <input
                                value={qtyValue}
                                onChange={(event) =>
                                  setPartQtyByKey((prev) => ({
                                    ...prev,
                                    [key]: event.target.value.replace(/[^0-9]/g, ""),
                                  }))
                                }
                                className="w-16 bg-black border border-slate-700 p-2 rounded text-xs"
                                placeholder="Qty"
                              />
                              <button
                                type="button"
                                disabled={!canUse || usingPartKey === key}
                                onClick={() => handleUsePart(step.step_id, part)}
                                className={`px-3 py-2 rounded text-xs font-semibold ${
                                  canUse
                                    ? "bg-cummins-red/30 border border-cummins-red hover:bg-cummins-red/40"
                                    : "bg-slate-900 border border-slate-800 text-slate-500"
                                }`}
                              >
                                {usingPartKey === key
                                  ? "Saving..."
                                  : canUse
                                    ? "Use Part"
                                    : "Out / Unknown"}
                              </button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}

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
                        disabled
                        onChange={(event) =>
                          setStepManualEscalation((prev) => ({
                            ...prev,
                            [step.step_id]: event.target.checked,
                          }))
                        }
                      />
                      Supervisor routing disabled in this flow
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
                      Did Not Work
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      )}

      {activeMenu === "job" && partsUsage.length > 0 && (
        <section className="bg-slate-900 border border-slate-800 p-4 rounded-xl space-y-2">
          <div className="text-sm font-semibold">Parts Used On This Job</div>
          <div className="space-y-2">
            {partsUsage.slice(0, 12).map((entry) => (
              <div
                key={`parts-usage-${entry.id}`}
                className="border border-slate-800 rounded p-2 text-xs"
              >
                <div className="font-mono text-slate-500">{entry.ts}</div>
                <div className="text-slate-200">
                  {entry.part_name_snapshot || entry.part_id} x{entry.quantity_used}
                </div>
                <div className="text-slate-400">
                  Step: {entry.step_id} | Location: {entry.location}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

    </div>
  );
}
