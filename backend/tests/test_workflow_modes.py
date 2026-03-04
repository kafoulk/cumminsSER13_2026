from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.local_db import db
from backend.main import (
    JobSubmitRequest,
    SupervisorApproveRequest,
    create_job,
    get_job_workflow,
    supervisor_approve,
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

        self.assertTrue(body["requires_approval"])
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

    def test_second_occurrence_can_produce_fix_plan(self) -> None:
        payload = {
            "equipment_id": "EQ-1001",
            "fault_code": "P0217",
            "symptoms": "Engine temp rising under load",
            "notes": "Coolant smell near radiator",
            "location": "Indy Yard",
        }
        create_job(JobSubmitRequest(**payload))
        body = create_job(JobSubmitRequest(**payload))

        self.assertFalse(body["requires_approval"])
        self.assertEqual(body["workflow_mode"], "FIX_PLAN")
        self.assertFalse(body["suppressed_guidance"])
        self.assertGreater(len(body["initial_workflow"]), 0)
        self.assertTrue(
            any(step.get("recommended_parts") for step in body["initial_workflow"]),
            "Expected at least one fix-plan step with recommended parts",
        )

    def test_approval_promotes_investigation_to_fix_plan(self) -> None:
        created_body = create_job(
            JobSubmitRequest(
                equipment_id="EQ-7777",
                fault_code="P0217",
                symptoms="Coolant leaking and smoke",
                notes="Very dangerous for operator",
                location="Remote site",
            )
        )
        self.assertTrue(created_body["requires_approval"])
        self.assertEqual(created_body["workflow_mode"], "INVESTIGATION_ONLY")

        approved_body = supervisor_approve(
            SupervisorApproveRequest(
                job_id=created_body["job_id"],
                approver_name="Supervisor A",
                decision="approve",
                notes="Proceed with controlled repair plan.",
            )
        )
        self.assertEqual(approved_body["status"], "READY")
        self.assertEqual(approved_body["workflow_mode"], "FIX_PLAN")

        workflow_body = get_job_workflow(created_body["job_id"])
        self.assertEqual(workflow_body["workflow_mode"], "FIX_PLAN")
        self.assertFalse(workflow_body["suppressed_guidance"])
        self.assertTrue(
            any(
                "parts to validate if failed" in str(step.get("instructions", "")).lower()
                and "suppressed pending supervisor decision" not in str(step.get("instructions", "")).lower()
                for step in workflow_body["workflow_steps"]
            ),
            "Expected fix-plan workflow instructions after approval",
        )


if __name__ == "__main__":
    unittest.main()
