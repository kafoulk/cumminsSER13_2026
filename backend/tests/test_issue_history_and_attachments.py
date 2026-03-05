from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from backend.local_db import db
from backend.main import (
    AttachmentUploadRequest,
    JobSubmitRequest,
    create_job,
    get_issue_history,
    get_job,
    get_job_attachments,
    get_similar_issues,
    upload_job_attachment,
)


class IssueHistoryAttachmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        db.LOCAL_DB_PATH = root / "local.db"
        db.SERVER_DB_PATH = root / "server.db"
        db.init_db()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_issue_history_and_similarity(self) -> None:
        payload = {
            "equipment_id": "EQ-4001",
            "fault_code": "P0217",
            "symptoms": "Engine temp rises under load",
            "notes": "Coolant smell near radiator",
            "location": "Indy Yard",
        }
        first = create_job(JobSubmitRequest(**payload))
        second = create_job(JobSubmitRequest(**payload))

        history = get_issue_history(fault_code="P0217", limit=20)
        self.assertGreaterEqual(history["count"], 2)
        self.assertTrue(any(item.get("job_id") == first["job_id"] for item in history["issues"]))

        similar = get_similar_issues(second["job_id"], limit=5)
        self.assertGreaterEqual(similar["count"], 1)
        self.assertTrue(any(item.get("job_id") == first["job_id"] for item in similar["similar_issues"]))

    def test_attachment_upload_persists_and_is_returned(self) -> None:
        created = create_job(
            JobSubmitRequest(
                equipment_id="EQ-9000",
                fault_code="P0217",
                symptoms="Engine temp rising",
                notes="Need photo evidence",
                location="Indy Yard",
            )
        )
        step_id = created["initial_workflow"][0]["step_id"]
        image_bytes = b"\x89PNG\r\n\x1a\n" + (b"x" * 256)
        encoded = base64.b64encode(image_bytes).decode("ascii")

        uploaded = upload_job_attachment(
            created["job_id"],
            AttachmentUploadRequest(
                step_id=step_id,
                source="camera",
                filename="coolant-leak.png",
                mime_type="image/png",
                image_base64=encoded,
                caption="Coolant near radiator seam",
            ),
        )
        self.assertEqual(uploaded["job_id"], created["job_id"])
        self.assertEqual(uploaded["step_id"], step_id)
        self.assertTrue(uploaded["attachment"].get("content_url"))

        attachments = get_job_attachments(created["job_id"])
        self.assertEqual(attachments["count"], 1)
        self.assertEqual(attachments["attachments"][0]["step_id"], step_id)

        job = get_job(created["job_id"])
        self.assertEqual(len(job.get("attachments", [])), 1)

        with db.open_local_connection() as local_conn:
            issue = db.get_issue_record(local_conn, created["job_id"])
        self.assertIsNotNone(issue)
        self.assertEqual(int((issue or {}).get("attachment_count", 0)), 1)


if __name__ == "__main__":
    unittest.main()
