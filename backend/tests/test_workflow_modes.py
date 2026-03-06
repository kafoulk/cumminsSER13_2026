from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.local_db import db
from backend.main import (
    CustomerApprovalRequest,
    JobSubmitRequest,
    QuoteEmailDraftRequest,
    WorkflowStepUpdateRequest,
    create_job,
    draft_quote_email,
    generate_quote,
    get_job_workflow,
    record_customer_approval,
    update_workflow_step,
)


class WorkflowModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        db.LOCAL_DB_PATH = root / "local.db"
        db.SERVER_DB_PATH = root / "server.db"
        db.init_db()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_free_text_only_submission_normalizes_payload(self) -> None:
        body = create_job(
            JobSubmitRequest(
                issue_text="Engine temp climbs under load and I smell coolant near radiator.",
            )
        )
        self.assertIn("job_id", body)
        self.assertIn("initial_workflow", body)
        self.assertEqual(body.get("issue_text"), "Engine temp climbs under load and I smell coolant near radiator.")
        self.assertTrue(isinstance(body.get("normalization_meta"), dict))
        self.assertNotIn("first_occurrence_fault", body.get("escalation_reasons", []))

        with db.open_local_connection() as local_conn:
            job = db.get_job(local_conn, body["job_id"])
        self.assertIsNotNone(job)
        payload = (job or {}).get("field_payload_json") or {}
        self.assertEqual(payload.get("equipment_id"), "UNKNOWN_EQUIPMENT")
        self.assertEqual(payload.get("fault_code"), "UNKNOWN_FAULT")
        self.assertTrue(str(payload.get("symptoms", "")).strip())
        self.assertTrue(str(payload.get("notes", "")).strip())

    def test_escalated_job_returns_investigation_only_mode(self) -> None:
        body = create_job(
            JobSubmitRequest(
                equipment_id="EQ-9001",
                fault_code="BRK-404",
                symptoms="Brake warning and smoke",
                notes="Potentially dangerous for operator. Stop operation immediately.",
                location="Depot",
            )
        )

        self.assertFalse(body["requires_approval"])
        self.assertEqual(body["status"], "DIAGNOSTIC_IN_PROGRESS")
        self.assertEqual(body["workflow_mode"], "INVESTIGATION_ONLY")
        self.assertTrue(body["suppressed_guidance"])
        self.assertIn("suppressed", body["service_report"].lower())
        for step in body["initial_workflow"]:
            self.assertEqual(step.get("suppressed"), 1)
            self.assertEqual(
                step.get("step_kind"),
                "investigate" if step.get("agent_id") == "triage_agent" else step.get("step_kind"),
            )
            self.assertEqual(step.get("recommended_parts", []), [])

    def test_second_occurrence_stays_in_diagnostic_stage_until_customer_approval(self) -> None:
        payload = {
            "equipment_id": "EQ-1001",
            "fault_code": "P0217",
            "symptoms": "Engine temp rising under load",
            "notes": "Coolant smell near radiator",
            "location": "Indy Yard",
        }
        create_job(JobSubmitRequest(**payload))
        body = create_job(JobSubmitRequest(**payload))

        self.assertEqual(body["status"], "DIAGNOSTIC_IN_PROGRESS")
        self.assertFalse(body["requires_approval"])
        self.assertEqual(body["workflow_mode"], "INVESTIGATION_ONLY")
        self.assertTrue(body["suppressed_guidance"])
        self.assertGreater(len(body["initial_workflow"]), 0)
        self.assertEqual(body.get("workflow_generation_agent"), "gathering_agent")

    def test_customer_approval_promotes_diagnostic_to_fix_plan(self) -> None:
        created_body = create_job(
            JobSubmitRequest(
                equipment_id="EQ-7777",
                fault_code="P0217",
                symptoms="Coolant leaking and smoke",
                notes="Very dangerous for operator",
                location="Remote site",
            )
        )
        self.assertEqual(created_body["workflow_mode"], "INVESTIGATION_ONLY")
        self.assertEqual(created_body["status"], "DIAGNOSTIC_IN_PROGRESS")

        generate_quote(created_body["job_id"])
        drafted = draft_quote_email(
            created_body["job_id"],
            QuoteEmailDraftRequest(recipient_name="Fleet Lead", recipient_email="fleet@example.com"),
        )
        self.assertEqual(drafted["status"], "AWAITING_CUSTOMER_APPROVAL")

        approved_body = record_customer_approval(
            created_body["job_id"],
            CustomerApprovalRequest(
                decision="approve",
                actor_id="field_technician",
                notes="Customer approved quote.",
            ),
        )
        self.assertEqual(approved_body["status"], "REPAIR_POOL_OPEN")
        self.assertEqual(approved_body["workflow_mode"], "FIX_PLAN")

        workflow_body = get_job_workflow(created_body["job_id"])
        self.assertEqual(workflow_body["workflow_mode"], "FIX_PLAN")
        self.assertFalse(workflow_body["suppressed_guidance"])
        self.assertTrue(
            any(
                "check these parts if needed" in str(step.get("instructions", "")).lower()
                and "do not repair yet" not in str(step.get("instructions", "")).lower()
                for step in workflow_body["workflow_steps"]
            ),
            "Expected fix-plan workflow instructions after approval",
        )

    def test_workflow_generation_delegates_to_gathering_agent(self) -> None:
        created = create_job(
            JobSubmitRequest(
                equipment_id="EQ-8888",
                fault_code="BRK-404",
                symptoms="Brake warning and smoke",
                notes="Potentially dangerous for operator. Stop operation immediately.",
                location="Depot",
            )
        )
        with db.open_local_connection() as local_conn:
            job_bundle = db.fetch_job_with_logs(local_conn, created["job_id"])
        self.assertIsNotNone(job_bundle)
        logs = (job_bundle or {}).get("decision_log", [])
        workflow_logs = [entry for entry in logs if str(entry.get("action", "")).upper() == "WORKFLOW_GENERATED"]
        self.assertTrue(workflow_logs)
        self.assertEqual(workflow_logs[-1].get("agent_id"), "gathering_agent")

    def test_repair_agent_generates_post_customer_approval_plan(self) -> None:
        payload = {
            "equipment_id": "EQ-1001",
            "fault_code": "P0217",
            "symptoms": "Engine temp rising under load",
            "notes": "Coolant smell near radiator",
            "location": "Indy Yard",
        }
        create_job(JobSubmitRequest(**payload))
        created = create_job(JobSubmitRequest(**payload))
        generate_quote(created["job_id"])
        draft_quote_email(
            created["job_id"],
            QuoteEmailDraftRequest(recipient_name="Fleet Lead", recipient_email="fleet@example.com"),
        )
        record_customer_approval(
            created["job_id"],
            CustomerApprovalRequest(decision="approve", actor_id="field_technician", notes="Approved."),
        )
        with db.open_local_connection() as local_conn:
            job_bundle = db.fetch_job_with_logs(local_conn, created["job_id"])
        self.assertIsNotNone(job_bundle)
        logs = (job_bundle or {}).get("decision_log", [])
        repair_open_logs = [entry for entry in logs if str(entry.get("action", "")).upper() == "REPAIR_POOL_OPENED"]
        self.assertTrue(repair_open_logs)
        self.assertEqual(repair_open_logs[-1].get("agent_id"), "repair_agent")

    def test_offline_step_alias_is_accepted_for_step_updates(self) -> None:
        created_body = create_job(
            JobSubmitRequest(
                equipment_id="EQ-4444",
                fault_code="P0217",
                symptoms="Coolant smell and temp rise",
                notes="Verify stabilization checks",
                location="Field Site",
            )
        )
        body = update_workflow_step(
            created_body["job_id"],
            WorkflowStepUpdateRequest(
                step_id="offline-context-observation",
                status="done",
                notes="Observation confirmed and logged.",
                measurement_json={"value": "stable"},
            ),
        )
        self.assertEqual(body["job_id"], created_body["job_id"])
        self.assertEqual(body["updated_step"]["step_id"], "step-context-observation")


if __name__ == "__main__":
    unittest.main()
