const LOCAL_OFFLINE_MODEL = "mobile-local-lite:3b-sim";
const ONLINE_MODEL = "llama3.1:8b";
const GOVERNANCE_POLICY_VERSION = "apex_governance_v2";
const ESCALATION_POLICY_VERSION = "mobile_local_policy_v1";

const SAFETY_TERMS = [
  "danger",
  "dangerous",
  "unsafe",
  "hazard",
  "smoke",
  "fire",
  "brake",
  "injury",
  "critical",
  "leaking",
  "leak",
];

const WARRANTY_TERMS = ["warranty", "claim", "coverage", "authorization"];

function nowIso() {
  return new Date().toISOString();
}

function clamp(value) {
  return Math.max(0, Math.min(1, Number(value || 0)));
}

function containsAny(text, terms) {
  return terms.some((term) => text.includes(term));
}

function collectMatchedTerms(text, terms) {
  const found = [];
  for (const term of terms) {
    if (text.includes(term)) found.push(term);
  }
  return found;
}

function inferTriage(payload, text) {
  const fault = String(payload?.fault_code || "").trim();
  let likelyCauses = ["Sensor fault", "Intermittent electrical issue"];
  let nextSteps = [
    "Capture active DTC data and freeze-frame.",
    "Inspect harness/connectors for the affected subsystem.",
    "Run targeted subsystem functional checks.",
  ];

  if (text.includes("temp") || text.includes("overheat") || text.includes("coolant")) {
    likelyCauses = ["Coolant flow restriction", "Thermostat/fan control issue"];
    nextSteps = [
      "Check coolant level and system pressure.",
      "Inspect radiator airflow, fan clutch, and thermostat behavior.",
      "Scan ECU events for thermal derate history.",
    ];
  } else if (text.includes("brake")) {
    likelyCauses = ["Hydraulic pressure instability", "Brake sensor/module fault"];
    nextSteps = [
      "Isolate vehicle and validate safe operating condition.",
      "Inspect brake line integrity and pressure readings.",
      "Run ABS/brake diagnostics and confirm warning source.",
    ];
  } else if (text.includes("fuel") || text.includes("power loss") || text.includes("injector")) {
    likelyCauses = ["Fuel delivery restriction", "Injector/electrical fault"];
    nextSteps = [
      "Measure fuel pressure under load.",
      "Inspect filters and fuel line restrictions.",
      "Validate injector harness and control signals.",
    ];
  }

  const safetyFlag = containsAny(text, SAFETY_TERMS);
  let confidence = 0.58;
  if (fault) confidence += 0.08;
  if (String(payload?.symptoms || "").trim().length > 15) confidence += 0.05;
  if (safetyFlag) confidence -= 0.06;

  return {
    summary: `Offline local triage for fault '${fault || "N/A"}' suggests ${likelyCauses[0].toLowerCase()} as most probable.`,
    likely_causes: likelyCauses,
    next_steps: nextSteps,
    safety_flag: safetyFlag,
    confidence: clamp(confidence),
  };
}

function inferEvidence(payload, triage, text) {
  let parts = ["Harness repair kit", "Primary sensor", "Connector seal kit"];
  if (text.includes("coolant") || text.includes("overheat")) {
    parts = ["Thermostat", "Water pump", "Coolant hose set"];
  } else if (text.includes("brake")) {
    parts = ["Brake pressure sensor", "Brake line kit", "ABS module"];
  } else if (text.includes("fuel")) {
    parts = ["Fuel filter", "Fuel pressure sensor", "Injector harness"];
  }

  return {
    manual_refs: [
      {
        title: "On-device Fallback Guide",
        path: "local://offline-fallback-manual",
        snippet: "Generated from local heuristics while network/backend is unavailable.",
        score: 1,
      },
    ],
    parts_candidates: parts,
    evidence_notes:
      "Generated from local on-device fallback logic; queued for backend reconciliation.",
    source_chunks_used: ["offline:heuristic:rulebase"],
    confidence: clamp((Number(triage?.confidence || 0.6) + 0.18)),
  };
}

function inferSchedule(requiresApproval, triage) {
  const priority = requiresApproval || triage?.safety_flag ? "HIGH" : "NORMAL";
  return {
    priority_hint: priority,
    eta_bucket: priority === "HIGH" ? "0-4h" : "12-24h",
    assignment_recommendation: {
      tech_id: "LOCAL-OFFLINE",
      tech_name: "On-device fallback assignment",
      rationale: "Temporary assignment until backend sync confirms final dispatch.",
    },
  };
}

function detectDomain(text) {
  if (text.includes("brake") || text.includes("abs") || text.includes("hydraulic")) {
    return "brake";
  }
  if (text.includes("coolant") || text.includes("overheat") || text.includes("thermostat") || text.includes("fan")) {
    return "cooling";
  }
  if (text.includes("fuel") || text.includes("injector") || text.includes("rail")) {
    return "fuel";
  }
  if (text.includes("oil") || text.includes("pressure") || text.includes("pump")) {
    return "lubrication";
  }
  return "general";
}

function buildPlaybook(domain, safetyFlag) {
  const generic = [
    {
      step_id: "general-baseline",
      title: "ECU Fault Baseline",
      instructions: "Capture active DTC and freeze-frame context.",
      required_inputs: ["active_dtcs", "freeze_frame", "operating_context"],
      pass_criteria: ["Fault context captured", "Baseline recorded"],
      risk_level: "MEDIUM",
    },
    {
      step_id: "general-physical",
      title: "Physical/Harness Inspection",
      instructions: "Inspect connector integrity, harness routing, and visible component condition.",
      required_inputs: ["connector_notes", "harness_notes", "visual_condition_notes"],
      pass_criteria: ["Inspection complete", "Fault area narrowed"],
      risk_level: "MEDIUM",
    },
    {
      step_id: "general-functional",
      title: "Targeted Functional Check",
      instructions: "Run a targeted subsystem test to validate suspected root cause.",
      required_inputs: ["test_procedure", "test_result", "measurement_value"],
      pass_criteria: ["Test completed", "Outcome recorded"],
      risk_level: "MEDIUM",
    },
  ];

  if (domain === "cooling") {
    return [
      {
        step_id: "cooling-baseline",
        title: "Baseline Thermal Snapshot",
        instructions: "Capture engine temperature trend under idle and load.",
        required_inputs: ["engine_temp", "ambient_temp", "engine_load_pct"],
        pass_criteria: ["Trend captured", "No uncontrolled thermal spike"],
        risk_level: safetyFlag ? "HIGH" : "MEDIUM",
      },
      {
        step_id: "cooling-integrity",
        title: "Cooling Circuit Integrity",
        instructions: "Pressure test cooling loop and inspect for leaks.",
        required_inputs: ["cooling_pressure_psi", "coolant_level_state", "leak_notes"],
        pass_criteria: ["Pressure stable", "No active leak source"],
        risk_level: "HIGH",
      },
      {
        step_id: "cooling-airflow",
        title: "Fan and Airflow Validation",
        instructions: "Verify fan engagement and radiator airflow path.",
        required_inputs: ["fan_state", "airflow_notes", "radiator_condition"],
        pass_criteria: ["Fan response confirmed", "Airflow path clear"],
        risk_level: "MEDIUM",
      },
    ];
  }
  if (domain === "brake") {
    return [
      {
        step_id: "brake-safety",
        title: "Immediate Safety Isolation",
        instructions: "Lockout operation and isolate unit before diagnostics.",
        required_inputs: ["lockout_status", "hazard_assessment", "supervisor_notified"],
        pass_criteria: ["Isolation confirmed", "Supervisor notified"],
        risk_level: "CRITICAL",
      },
      {
        step_id: "brake-pressure",
        title: "Brake Pressure Validation",
        instructions: "Measure line pressure stability and inspect for drop/leak.",
        required_inputs: ["line_pressure", "pressure_drop_result", "leak_notes"],
        pass_criteria: ["Pressure stable", "No severe leak"],
        risk_level: "HIGH",
      },
      {
        step_id: "brake-controls",
        title: "ABS/Control Module Diagnostics",
        instructions: "Read ABS faults and validate sensor/module communication.",
        required_inputs: ["abs_dtcs", "sensor_signal_state", "module_comm_state"],
        pass_criteria: ["Fault localized", "Control path validated"],
        risk_level: "HIGH",
      },
    ];
  }
  if (domain === "fuel") {
    return [
      {
        step_id: "fuel-baseline",
        title: "Fuel Delivery Baseline",
        instructions: "Capture rail pressure and throttle response behavior.",
        required_inputs: ["fuel_rail_pressure", "throttle_response_notes", "load_condition"],
        pass_criteria: ["Pressure trend recorded", "Delivery behavior documented"],
        risk_level: "MEDIUM",
      },
      {
        step_id: "fuel-restriction",
        title: "Restriction/Filter Check",
        instructions: "Inspect filters and lines for flow restriction.",
        required_inputs: ["filter_condition", "line_restriction_notes", "flow_assessment"],
        pass_criteria: ["No critical restriction", "Flow path validated"],
        risk_level: "MEDIUM",
      },
      {
        step_id: "fuel-electrical",
        title: "Injector/Harness Verification",
        instructions: "Validate injector command quality and harness continuity.",
        required_inputs: ["injector_command_state", "harness_continuity", "connector_condition"],
        pass_criteria: ["Injector command valid", "Harness integrity confirmed"],
        risk_level: "HIGH",
      },
    ];
  }
  if (domain === "lubrication") {
    return [
      {
        step_id: "lube-baseline",
        title: "Oil Pressure Baseline",
        instructions: "Compare sensor pressure with mechanical gauge reading.",
        required_inputs: ["sensor_pressure", "mechanical_pressure", "engine_state"],
        pass_criteria: ["Readings correlated", "Pressure in acceptable range"],
        risk_level: safetyFlag ? "HIGH" : "MEDIUM",
      },
      {
        step_id: "lube-integrity",
        title: "Oil Circuit Integrity",
        instructions: "Inspect oil level, grade, filter condition, and leaks.",
        required_inputs: ["oil_level", "oil_grade", "filter_condition", "leak_notes"],
        pass_criteria: ["Oil circuit validated", "No critical leak"],
        risk_level: "MEDIUM",
      },
    ];
  }
  return generic;
}

function composeActionableInstruction(step, recommendedParts, suppressRepairGuidance = false) {
  const captureLine = `Capture: ${(step.required_inputs || []).join(", ") || "observation_notes"}.`;
  const passLine = `Pass when: ${(step.pass_criteria || []).join(", ") || "Result recorded"}.`;
  const partsLine = suppressRepairGuidance
    ? "Repair and parts guidance suppressed pending supervisor decision."
    : `Parts to validate if failed: ${(recommendedParts || []).join(", ") || "none listed"}.`;
  const escalateLine = suppressRepairGuidance
    ? "If blocked/failed: capture evidence and escalate to supervisor."
    : step.risk_level === "HIGH" || step.risk_level === "CRITICAL"
      ? "If blocked/failed: escalate to supervisor immediately."
      : "If blocked/failed: record evidence and request replan/supervisor review if needed.";
  return `${step.instructions} ${captureLine} ${passLine} ${partsLine} ${escalateLine}`;
}

function buildWorkflow(payload, triage, evidence, schedule, workflowMode) {
  const suppressRepairGuidance = workflowMode === "INVESTIGATION_ONLY";
  const text = [
    payload?.fault_code,
    payload?.symptoms,
    payload?.notes,
    triage?.summary,
    ...(triage?.likely_causes || []),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  const domain = detectDomain(text);
  const playbook = buildPlaybook(domain, Boolean(triage?.safety_flag));
  const fallbackParts = evidence?.parts_candidates || [];

  const steps = playbook.map((step, index) => {
    const recommendedParts = suppressRepairGuidance ? [] : fallbackParts.slice(0, 3);
    return {
      step_id: step.step_id,
      step_order: index + 1,
      title: step.title,
      instructions: composeActionableInstruction(step, recommendedParts, suppressRepairGuidance),
      required_inputs: step.required_inputs || [],
      pass_criteria: step.pass_criteria || [],
      risk_level: step.risk_level || "MEDIUM",
      status: "pending",
      recommended_parts: recommendedParts,
      step_kind: suppressRepairGuidance ? "investigate" : "diagnose",
      suppressed: suppressRepairGuidance ? 1 : 0,
      agent_id: "triage_agent",
    };
  });

  const observationText = [
    payload?.symptoms ? `Symptoms: ${payload.symptoms}` : "",
    payload?.notes ? `Notes: ${payload.notes}` : "",
  ]
    .filter(Boolean)
    .join(" | ")
    .slice(0, 260);
  if (observationText) {
    const observationRisk =
      triage?.safety_flag || containsAny(observationText.toLowerCase(), SAFETY_TERMS) ? "HIGH" : "MEDIUM";
    steps.push({
      step_id: "offline-context-observation",
      step_order: steps.length + 1,
      title: "Technician Observation Validation",
      instructions: composeActionableInstruction(
        {
          instructions: `Validate this field observation against diagnostics: "${observationText}".`,
          required_inputs: ["observation_confirmation", "variance_notes"],
          pass_criteria: ["Observation reconciled", "Variance documented if present"],
          risk_level: observationRisk,
        },
        suppressRepairGuidance ? [] : fallbackParts.slice(0, 3),
        suppressRepairGuidance
      ),
      required_inputs: ["observation_confirmation", "variance_notes"],
      pass_criteria: ["Observation reconciled", "Variance documented if present"],
      risk_level: observationRisk,
      status: "pending",
      recommended_parts: suppressRepairGuidance ? [] : fallbackParts.slice(0, 3),
      step_kind: "investigate",
      suppressed: suppressRepairGuidance ? 1 : 0,
      agent_id: "triage_agent",
    });
  }

  steps.push({
    step_id: "offline-schedule-checkpoint",
    step_order: steps.length + 1,
    title: "Scheduling and Dispatch Checkpoint",
    instructions: composeActionableInstruction(
      {
        instructions: `Confirm dispatch priority ${schedule?.priority_hint || "NORMAL"} and ETA ${schedule?.eta_bucket || "12-24h"}.`,
        required_inputs: ["dispatch_confirmation", "eta_commitment"],
        pass_criteria: ["Dispatch confirmed", "ETA communicated"],
        risk_level: "LOW",
      },
      [],
      suppressRepairGuidance
    ),
    required_inputs: ["dispatch_confirmation", "eta_commitment"],
    pass_criteria: ["Dispatch confirmed", "ETA communicated"],
    risk_level: "LOW",
    status: "pending",
    recommended_parts: [],
    step_kind: "handoff",
    suppressed: suppressRepairGuidance ? 1 : 0,
    agent_id: "scheduler_agent",
  });

  steps.push({
    step_id: suppressRepairGuidance ? "offline-supervisor-handoff" : "offline-final-handoff",
    step_order: steps.length + 1,
    title: suppressRepairGuidance ? "Supervisor Evidence Handoff" : "Final Handoff and Report Lock",
    instructions: composeActionableInstruction(
      {
        instructions: suppressRepairGuidance
          ? "Finalize evidence summary and unresolved questions for supervisor review. Do not perform repair actions until approved."
          : "Finalize diagnosis summary, confirm parts readiness, and prepare supervisor/back-office handoff.",
        required_inputs: suppressRepairGuidance
          ? ["evidence_summary", "open_questions", "handoff_notes"]
          : ["repair_plan_summary", "parts_confirmation", "handoff_notes"],
        pass_criteria: suppressRepairGuidance
          ? ["Evidence package complete", "Supervisor package submitted"]
          : ["Service report finalized", "Handoff ready for sync"],
        risk_level: "MEDIUM",
      },
      suppressRepairGuidance ? [] : fallbackParts.slice(0, 3),
      suppressRepairGuidance
    ),
    required_inputs: suppressRepairGuidance
      ? ["evidence_summary", "open_questions", "handoff_notes"]
      : ["repair_plan_summary", "parts_confirmation", "handoff_notes"],
    pass_criteria: suppressRepairGuidance
      ? ["Evidence package complete", "Supervisor package submitted"]
      : ["Service report finalized", "Handoff ready for sync"],
    risk_level: "MEDIUM",
    status: "pending",
    recommended_parts: suppressRepairGuidance ? [] : fallbackParts.slice(0, 3),
    step_kind: "handoff",
    suppressed: suppressRepairGuidance ? 1 : 0,
    agent_id: "orchestrator",
  });

  return steps;
}

function buildServiceReport(
  payload,
  triage,
  evidence,
  schedule,
  requiresApproval,
  safetyHit,
  warrantyHit,
  workflowMode
) {
  const suppressRepairGuidance = workflowMode === "INVESTIGATION_ONLY";
  const parts = suppressRepairGuidance
    ? "Suppressed pending supervisor decision."
    : (evidence?.parts_candidates || []).join(", ") || "N/A";
  const manuals = (evidence?.manual_refs || []).map((item) => item.title).join(", ") || "N/A";
  const diagLines = (triage?.next_steps || []).map((item) => `- ${item}`).join("\n");

  return [
    "Customer complaint",
    `- Equipment: ${payload?.equipment_id || "N/A"}`,
    `- Fault code: ${payload?.fault_code || "N/A"}`,
    `- Symptoms: ${payload?.symptoms || "N/A"}`,
    "",
    "Observations",
    `- ${triage?.summary || "Offline local triage pending."}`,
    "",
    "Diagnostics performed",
    diagLines || "- Offline local diagnostics generated.",
    "",
    "Manual references used",
    `- ${manuals}`,
    "",
    "Parts considered",
    `- ${parts}`,
    "",
    "Actions taken (proposed)",
    ...(suppressRepairGuidance
      ? [
          "- Gather diagnostic evidence requested in checklist.",
          "- Capture unresolved questions for supervisor review.",
          "- Repair guidance intentionally suppressed until approval.",
        ]
      : [`- Priority hint: ${schedule?.priority_hint || "N/A"}`, `- ETA bucket: ${schedule?.eta_bucket || "N/A"}`]),
    "",
    "Safety/warranty notes",
    safetyHit ? "- Safety signal detected in local offline analysis." : "- No safety signal detected.",
    warrantyHit ? "- Warranty signal detected in local offline analysis." : "- No warranty signal detected.",
    requiresApproval ? "- Supervisor approval required before release." : "- Supervisor approval not required.",
    "",
    "Next steps",
    ...(suppressRepairGuidance
      ? [
          "- Complete investigation checklist and evidence package.",
          "- Submit supervisor review package.",
          "- Wait for supervisor decision before attempting fixes.",
        ]
      : [
          "- Continue field diagnostics using generated workflow.",
          "- Sync queued event when network returns.",
          "- Reconcile with backend orchestrator output.",
        ]),
  ].join("\n");
}

export function getLocalRuntimeConfig(isOffline) {
  if (isOffline) {
    return {
      mode_effective: "offline",
      model_online: ONLINE_MODEL,
      model_offline: LOCAL_OFFLINE_MODEL,
      model_selected: LOCAL_OFFLINE_MODEL,
      model_tier: "offline_1_3b",
      model_policy_valid: true,
      model_policy_notes: ["device_local_offline_fallback_active"],
    };
  }
  return {
    mode_effective: "online",
    model_online: ONLINE_MODEL,
    model_offline: LOCAL_OFFLINE_MODEL,
    model_selected: ONLINE_MODEL,
    model_tier: "online_8b",
    model_policy_valid: true,
    model_policy_notes: [],
  };
}

export function runLocalOfflineJob(payload, queueInfo = {}) {
  const issueText = String(payload?.issue_text || "").trim();
  const normalizedPayload = {
    ...(payload || {}),
    equipment_id: payload?.equipment_id || "UNKNOWN_EQUIPMENT",
    fault_code: payload?.fault_code || "UNKNOWN_FAULT",
    symptoms: payload?.symptoms || issueText || "No symptom details provided.",
    notes: payload?.notes || issueText || "No technician notes provided.",
    issue_text: issueText,
  };
  const text = [
    normalizedPayload?.issue_text,
    normalizedPayload?.fault_code,
    normalizedPayload?.symptoms,
    normalizedPayload?.notes,
    normalizedPayload?.location,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();

  const triage = inferTriage(normalizedPayload, text);
  const evidence = inferEvidence(normalizedPayload, triage, text);
  const safetyTerms = collectMatchedTerms(text, SAFETY_TERMS);
  const warrantyTerms = collectMatchedTerms(text, WARRANTY_TERMS);
  const safetyHit = safetyTerms.length > 0 || Boolean(triage?.safety_flag);
  const warrantyHit = warrantyTerms.length > 0;
  const manualRequest = Boolean(normalizedPayload?.request_supervisor_review);
  const combinedConfidence = clamp((Number(triage?.confidence || 0.6) + Number(evidence?.confidence || 0.7)) / 2);

  const escalationReasons = [];
  if (manualRequest) escalationReasons.push("manual_request");
  if (combinedConfidence < 0.7) escalationReasons.push("low_confidence");
  if (safetyHit) escalationReasons.push("safety_signal");
  if (warrantyHit) escalationReasons.push("warranty_signal");
  if (triage?.safety_flag) escalationReasons.push("triage_unsafe");

  const requiresApproval = escalationReasons.length > 0;
  const workflowMode = requiresApproval ? "INVESTIGATION_ONLY" : "FIX_PLAN";
  const scheduleHint = inferSchedule(requiresApproval, triage);
  const initialWorkflow = buildWorkflow(normalizedPayload, triage, evidence, scheduleHint, workflowMode);

  return {
    job_id: normalizedPayload?.job_id,
    status: requiresApproval ? "PENDING_APPROVAL" : "READY",
    requires_approval: requiresApproval,
    workflow_mode: workflowMode,
    workflow_intent:
      workflowMode === "INVESTIGATION_ONLY"
        ? "Collect additional evidence for supervisor decision. Repair guidance suppressed."
        : "Execute repair plan and verify fix.",
    allowed_actions:
      workflowMode === "INVESTIGATION_ONLY"
        ? ["capture_observation", "capture_measurement", "attach_evidence", "request_supervisor_review"]
        : ["diagnose", "replace_part", "repair", "verify_fix", "closeout"],
    suppressed_guidance: workflowMode === "INVESTIGATION_ONLY",
    approval_due_ts: null,
    timed_out: false,
    service_report: buildServiceReport(
      normalizedPayload,
      triage,
      evidence,
      scheduleHint,
      requiresApproval,
      safetyHit,
      warrantyHit,
      workflowMode
    ),
    triage,
    evidence,
    schedule_hint: scheduleHint,
    assignment_recommendation: scheduleHint.assignment_recommendation,
    initial_workflow: initialWorkflow,
    escalation_reasons: escalationReasons,
    risk_signals: {
      source: "device_local_fallback",
      confidence: combinedConfidence,
      safety_signal: safetyHit,
      warranty_signal: warrantyHit,
      matched_terms: {
        safety: safetyTerms,
        warranty: warrantyTerms,
      },
      rationale: "Generated via on-device local fallback model path while offline.",
    },
    escalation_policy_version: ESCALATION_POLICY_VERSION,
    governance_policy_version: GOVERNANCE_POLICY_VERSION,
    mode_effective: "offline",
    model_selected: LOCAL_OFFLINE_MODEL,
    model_online: ONLINE_MODEL,
    model_offline: LOCAL_OFFLINE_MODEL,
    model_tier: "offline_1_3b",
    model_policy_valid: true,
    model_policy_notes: ["device_local_offline_fallback_active"],
    local_only: true,
    queued_offline: true,
    queue_id: queueInfo?.queue_id ?? null,
    queued_at: queueInfo?.queued_at || nowIso(),
  };
}
