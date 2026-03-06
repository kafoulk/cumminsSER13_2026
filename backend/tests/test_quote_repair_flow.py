from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.local_db import db
from backend.main import (
    CustomerApprovalRequest,
    JobSubmitRequest,
    QuoteEmailDraftRequest,
    RepairClaimRequest,
    RepairCompleteRequest,
    claim_repair_job,
    complete_repair_job,
    create_job,
    draft_quote_email,
    generate_quote,
    get_repair_pool,
    get_supervisor_tickets,
    record_customer_approval,
)


class QuoteRepairFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        db.LOCAL_DB_PATH = root / "local.db"
        db.SERVER_DB_PATH = root / "server.db"
        db.init_db()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _create_ready_job(self) -> dict:
        payload = {
            "equipment_id": "EQ-5100",
            "fault_code": "P0217",
            "symptoms": "Engine temp rising under load",
            "notes": "Coolant smell near radiator",
            "location": "Indy Yard",
        }
        create_job(JobSubmitRequest(**payload))
        created = create_job(JobSubmitRequest(**payload))
        self.assertEqual(created["status"], "DIAGNOSTIC_IN_PROGRESS")
        return created

    def test_quote_to_repair_pool_lifecycle(self) -> None:
        created = self._create_ready_job()
        job_id = created["job_id"]

        quoted = generate_quote(job_id)
        self.assertEqual(quoted["job_id"], job_id)
        self.assertEqual(quoted["status"], "DIAGNOSTIC_IN_PROGRESS")
        self.assertGreater(float((quoted.get("quote_package") or {}).get("total_usd", 0.0)), 0.0)

        draft = draft_quote_email(
            job_id,
            QuoteEmailDraftRequest(recipient_name="Fleet Lead", recipient_email="fleet@example.com"),
        )
        self.assertEqual(draft["status"], "AWAITING_CUSTOMER_APPROVAL")
        self.assertTrue(draft.get("quote_email_draft", {}).get("subject"))

        customer_update = record_customer_approval(
            job_id,
            CustomerApprovalRequest(
                decision="approve",
                actor_id="field_technician",
                notes="Customer approved during callback.",
            ),
        )
        self.assertEqual(customer_update["status"], "REPAIR_POOL_OPEN")
        open_ledger = get_supervisor_tickets(ticket_state="OPEN", limit=200)
        self.assertTrue(any(item.get("job_id") == job_id for item in open_ledger.get("tickets", [])))

        pool = get_repair_pool(include_claimed=True, limit=20)
        self.assertTrue(any(item.get("job_id") == job_id for item in pool.get("jobs", [])))

        claimed = claim_repair_job(
            job_id,
            RepairClaimRequest(technician_id="tech-007", technician_name="Tech Seven"),
        )
        self.assertEqual(claimed["status"], "REPAIR_IN_PROGRESS")

        completed = complete_repair_job(
            job_id,
            RepairCompleteRequest(technician_id="tech-007", notes="Repair validated and test run passed."),
        )
        self.assertEqual(completed["status"], "REPAIR_COMPLETED")
        closed_ledger = get_supervisor_tickets(ticket_state="CLOSED", limit=200)
        self.assertTrue(any(item.get("job_id") == job_id for item in closed_ledger.get("tickets", [])))

    def test_repair_completion_does_not_escalate_to_supervisor(self) -> None:
        created = self._create_ready_job()
        job_id = created["job_id"]
        draft_quote_email(
            job_id,
            QuoteEmailDraftRequest(recipient_name="Fleet Lead", recipient_email="fleet@example.com"),
        )
        record_customer_approval(
            job_id,
            CustomerApprovalRequest(
                decision="approve",
                actor_id="field_technician",
                notes="Approved by customer.",
            ),
        )
        claim_repair_job(
            job_id,
            RepairClaimRequest(technician_id="tech-007", technician_name="Tech Seven"),
        )
        completed = complete_repair_job(
            job_id,
            RepairCompleteRequest(
                technician_id="tech-007",
                notes="High heat and smoke noted during repair validation, mitigated and completed.",
            ),
        )
        self.assertEqual(completed["status"], "REPAIR_COMPLETED")
        self.assertEqual(int(completed.get("requires_approval", 0)), 0)

    def test_schema_backfills_issue_records_for_legacy_jobs(self) -> None:
        with db.open_local_connection() as conn:
            conn.execute("DELETE FROM issue_records")
            conn.execute(
                """
                INSERT INTO jobs(
                    job_id, created_ts, updated_ts, status, field_payload_json,
                    final_response_json, requires_approval, approved_by, approved_ts,
                    guided_question, guided_answer, approval_due_ts, timed_out, first_occurrence_fault,
                    assigned_tech_id, workflow_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-job-1",
                    "2026-03-01T00:00:00Z",
                    "2026-03-01T00:05:00Z",
                    "READY",
                    json.dumps(
                        {
                            "equipment_id": "EQ-LEGACY",
                            "fault_code": "P0001",
                            "issue_text": "Legacy issue",
                            "symptoms": "legacy symptom",
                            "notes": "legacy notes",
                            "location": "Legacy Yard",
                        }
                    ),
                    json.dumps({"status": "READY", "workflow_mode": "FIX_PLAN"}),
                    0,
                    None,
                    None,
                    None,
                    None,
                    None,
                    0,
                    0,
                    None,
                    "FIX_PLAN",
                ),
            )
            conn.commit()
            db.create_schema(conn)
            conn.commit()
            issue = db.get_issue_record(conn, "legacy-job-1")
        self.assertIsNotNone(issue)
        self.assertEqual((issue or {}).get("equipment_id"), "EQ-LEGACY")


if __name__ == "__main__":
    unittest.main()
