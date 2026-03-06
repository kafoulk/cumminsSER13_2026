# Cummins Service Reboot (Mobile App + Backend)

This repository is now optimized for a **mobile app-first** workflow (iOS/Android via Capacitor) backed by FastAPI.

## Backend Overview

- FastAPI app and orchestrator: `backend/main.py`
- Agents:
  - `backend/agents/triage_agent.py`
  - `backend/agents/parts_agent.py`
  - `backend/agents/scheduler_agent.py` (minimal stub)
  - `backend/agents/quote_agent.py`
  - `backend/agents/email_agent.py`
- SQLite stores:
  - Local system-of-record: `backend/local_db/local.db`
  - Demo server store: `backend/local_db/server.db`
  - Outgoing sync queue in local DB (`sync_queue` table)
- Ollama config: `backend/config/ollama_config.yaml`
- Escalation/routing policy config: `backend/config/escalation_policy.yaml`
- Synthetic knowledge base: `backend/knowledge_base/manuals/*.txt`
- Structured playbooks: `backend/knowledge_base/synthetic/fault_playbooks.yaml`

## Install & Run Backend

```bash
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 9054
```

Backend URL: `http://127.0.0.1:9054`.

## Mobile App (Primary Path)

This project supports an installable app (iOS + Android) via Capacitor. Treat web dev server as secondary.

Install dependencies:

```bash
cd frontend
npm install
```

Build app web bundle + sync native shells:

```bash
npm run mobile:prepare
```

Open platform projects:

```bash
# iOS
npm run mobile:xcode

# Android
npm run mobile:android
```

Run directly on connected device/emulator:

```bash
npm run mobile:run:ios
npm run mobile:run:android
```

### iPhone Voice Input Setup

If voice input shows `not allowed`, use this sequence:

1. Re-sync native plugins:
   ```bash
   cd frontend
   npm run mobile:prepare
   ```
2. Open Xcode project:
   ```bash
   npm run mobile:xcode
   ```
3. Clean build folder in Xcode, then run on device again.
4. First voice tap should trigger iOS permission prompts for microphone/speech recognition.
5. If prompts were denied earlier, open iPhone **Settings > Cummins Service Reboot** and enable:
   - Microphone
   - Speech Recognition

Implementation details:
- Native voice path uses Capacitor speech plugin on app runtime.
- Browser speech API is retained as fallback for non-native web usage.
- iOS permission keys live in `frontend/ios/App/App/Info.plist`.

## Phone Demo Setup (App + Backend)

Start backend on LAN:

```bash
uvicorn backend.main:app --reload --host 0.0.0.0 --port 9054
```

In-app configuration:
1. Open **Settings** tab in the app.
2. Set **Backend Base URL** to `http://<YOUR_LAN_IP>:9054`.
3. Save and verify **Runtime Proof** values (`mode_effective`, `model_selected`, `model_tier`).
4. Model routing is automatic:
   - online network => `online_8b`
   - offline network => `offline_1_3b`

iOS local-network note:
- `frontend/ios/App/App/Info.plist` includes ATS allowances for local demo HTTP/LAN testing.
- For production, lock ATS/TLS policies down.

Offline/online replay demo:
1. Submit a job while online.
2. Disable network and submit another job.
3. App now runs an on-device offline local fallback inference path immediately (reduced quality, same response shape).
4. Verify queued count increases.
5. Re-enable network and tap **Replay Queue**.
6. Verify replayed job appears in backend/supervisor flows.

The UI includes:
- Technician page (`/`): submits jobs, runs actionable workflow steps, triggers replan, and shows decision/workflow logs.
- Customer approvals page (`/customer-approval`): simple queue for recording customer approve/decline decisions.
- Repair Pool page (`/repair-pool`): shows customer-approved tickets, lets technicians claim and complete work.
- Supervisor page (`/supervisor`): loads pending queue, approve/deny actions, sync trigger, and local agent metrics.
- Settings page (`/settings`): configures backend base URL and shows automatic mode/runtime diagnostics.
- Note: Supervisor queue shows jobs in `PENDING_APPROVAL`, `TIMEOUT_HOLD`, and `PENDING_QUOTE_APPROVAL` (not `READY` jobs).

## API Endpoints

- `GET /api/demo/scenarios`
- `POST /api/demo/history/reset`
- `POST /api/job/intake`
- `POST /api/job/{job_id}/guided-answer`
- `POST /api/job`
- `GET /api/job/{job_id}/timeline`
- `GET /api/job/{job_id}/workflow`
- `POST /api/job/{job_id}/workflow/step`
- `POST /api/job/{job_id}/replan`
- `POST /api/job/{job_id}/quote`
- `POST /api/job/{job_id}/quote/email-draft`
- `POST /api/job/{job_id}/customer-approval`
- `GET /api/customer/queue`
- `GET /api/repair/pool`
- `POST /api/repair/pool/{job_id}/claim`
- `POST /api/repair/pool/{job_id}/complete`
- `POST /api/job/{job_id}/attachments`
- `GET /api/job/{job_id}/attachments`
- `GET /api/attachments/{attachment_id}/content`
- `GET /api/supervisor/queue`
- `GET /api/supervisor/alerts`
- `POST /api/supervisor/approve`
- `POST /api/jobs/check-timeouts`
- `POST /api/sync`
- `GET /api/job/{job_id}`
- `GET /api/issues`
- `GET /api/issues/{job_id}/similar`
- `GET /api/metrics/agent-performance`
- `GET /api/config/runtime`
- `GET /api/health`

## Synthetic Job Example

```bash
curl -X POST http://127.0.0.1:9054/api/job \
  -H "Content-Type: application/json" \
  -d '{
    "issue_text": "Engine temp rises quickly under load and coolant smell is strong near radiator.",
    "equipment_id": "EQ-1001",
    "fault_code": "P0217",
    "location": "Indianapolis Yard"
  }'
```

## Similar-Issue Demo Seed (Reset + Preload)

Reset local/server demo DB and preload historical jobs:

```bash
curl -X POST http://127.0.0.1:9054/api/demo/history/reset \
  -H "Content-Type: application/json" \
  -d '{"clear_server": true}'
```

Then run these three preloaded demo patterns:
- Match example A: cooling over-temp (`P0217`) should return similar historical jobs.
- Match example B: coolant leak + over-temp (`P0217`) should return similar historical jobs.
- No-match example: electronics reboot (`ELEC-771`) should return no strong similar jobs.

You can also load these from the Technician `Demo tools` scenario dropdown.

Free-text-first behavior:
- `issue_text` is the primary field.
- `equipment_id` and `fault_code` are optional.
- If omitted, backend infers values from text or falls back to `UNKNOWN_*`.
- Mobile intake includes a voice input button with native iOS permissions flow.

Image evidence behavior:
- Technician can attach step-level images (`camera` or `gallery`) to a job.
- Limits: up to 5 images per step, max 3MB each.
- Supervisor queue includes attachment counts and supports in-app preview/download.
- Offline mode queues attachment metadata + file-copy events for replay to `server.db` mirror.

## Guided Learning Flow (APEX v2)

1) Intake returns a required guided diagnostic question:

```bash
curl -s -X POST http://127.0.0.1:9054/api/job/intake \
  -H "Content-Type: application/json" \
  -d '{
    "equipment_id": "EQ-9100",
    "fault_code": "P0217",
    "symptoms": "Coolant temp rise under load",
    "notes": "Need guided process",
    "location": "Indy Yard"
  }'
```

2) Submit answer to complete diagnosis:

```bash
curl -s -X POST http://127.0.0.1:9054/api/job/<JOB_ID>/guided-answer \
  -H "Content-Type: application/json" \
  -d '{
    "answer_text": "Confirmed fan not engaging consistently."
  }'
```

Compatibility note:
- `POST /api/job` still works for existing clients.
- If `guided_answer` is omitted on `POST /api/job`, backend logs a compatibility fallback answer (`GUIDED_COMPAT_FALLBACK_USED`).

## Quote -> Customer -> Repair Flow

1) Generate quote package:

```bash
curl -X POST http://127.0.0.1:9054/api/job/<JOB_ID>/quote
```

2) Generate customer email draft and route to supervisor queue:

```bash
curl -X POST http://127.0.0.1:9054/api/job/<JOB_ID>/quote/email-draft \
  -H "Content-Type: application/json" \
  -d '{
    "recipient_name": "Fleet Manager",
    "recipient_email": "fleet@example.com"
  }'
```

3) Supervisor approves/denies quote email with existing endpoint:

```bash
curl -X POST http://127.0.0.1:9054/api/supervisor/approve \
  -H "Content-Type: application/json" \
  -d '{
    "job_id": "<JOB_ID>",
    "approver_name": "Supervisor A",
    "decision": "approve",
    "notes": "Ready to send to customer."
  }'
```

4) Record customer decision:

```bash
curl -X POST http://127.0.0.1:9054/api/job/<JOB_ID>/customer-approval \
  -H "Content-Type: application/json" \
  -d '{
    "decision": "approve",
    "actor_id": "field_technician",
    "notes": "Customer approved over phone."
  }'
```

5) Claim from repair pool and complete:

```bash
curl http://127.0.0.1:9054/api/repair/pool

curl -X POST http://127.0.0.1:9054/api/repair/pool/<JOB_ID>/claim \
  -H "Content-Type: application/json" \
  -d '{"technician_id":"tech-001","technician_name":"Field Technician"}'

curl -X POST http://127.0.0.1:9054/api/repair/pool/<JOB_ID>/complete \
  -H "Content-Type: application/json" \
  -d '{"technician_id":"tech-001","notes":"Repair verified."}'
```

## Demo Scenario Endpoint

```bash
curl http://127.0.0.1:9054/api/demo/scenarios
```

## Offline Demo + Reconciliation

1) Start backend in forced offline mode:

```bash
OFFLINE=1 uvicorn backend.main:app --reload --port 9054
```

2) Submit a job while offline:

```bash
curl -X POST http://127.0.0.1:9054/api/job \
  -H "Content-Type: application/json" \
  -d '{
    "equipment_id": "EQ-2002",
    "fault_code": "BRK-404",
    "symptoms": "Brake warning and smoke near rear axle",
    "notes": "Potential safety hazard in service lane",
    "location": "Columbus Depot",
    "is_offline": true
  }'
```

3) Check supervisor queue:

```bash
curl http://127.0.0.1:9054/api/supervisor/queue
```

4) Reconnect (run without `OFFLINE=1`) and sync:

```bash
curl -X POST http://127.0.0.1:9054/api/sync
```

Offline queue policy for this build:
- Queue contains job snapshots, workflow replacements/events, escalation logs, metrics, and supervisor alerts.
- Sync replays queue entries into `server.db` and marks items as synced.

5) Fetch full job and audit log:

```bash
curl http://127.0.0.1:9054/api/job/<JOB_ID>
```

6) Fetch unified timeline (decision logs + workflow events):

```bash
curl http://127.0.0.1:9054/api/job/<JOB_ID>/timeline
```

7) Run timeout fail-safe checker (moves overdue approvals to `TIMEOUT_HOLD`):

```bash
curl -X POST http://127.0.0.1:9054/api/jobs/check-timeouts
```

8) View supervisor alerts (timeouts, sync failures):

```bash
curl http://127.0.0.1:9054/api/supervisor/alerts
```

## Supervisor Approval Example

```bash
curl -X POST http://127.0.0.1:9054/api/supervisor/approve \
  -H "Content-Type: application/json" \
  -d '{
    "job_id": "<JOB_ID>",
    "approver_name": "Supervisor A",
    "decision": "approve",
    "notes": "Proceed with controlled repair plan."
  }'
```

Approval behavior:
- If decision is `approve`, backend automatically regenerates workflow in `FIX_PLAN` mode.
- If decision is `deny`, job remains non-repair (`DENIED`) with investigation-only guidance.

## Workflow Step Update Example

```bash
curl -X POST http://127.0.0.1:9054/api/job/<JOB_ID>/workflow/step \
  -H "Content-Type: application/json" \
  -d '{
    "step_id": "step-1",
    "status": "done",
    "measurement_json": {"temp_f": 192},
    "notes": "Cooling temp normalized after fan engagement",
    "actor_id": "field_technician",
    "request_supervisor_review": true
  }'
```

## Attachment Upload Example

```bash
curl -X POST http://127.0.0.1:9054/api/job/<JOB_ID>/attachments \
  -H "Content-Type: application/json" \
  -d '{
    "step_id": "step-1",
    "source": "camera",
    "filename": "coolant-leak.jpg",
    "mime_type": "image/jpeg",
    "image_base64": "<BASE64_IMAGE_BYTES>",
    "caption": "Leak near upper hose clamp"
  }'
```

## Issue History Search Example

```bash
curl "http://127.0.0.1:9054/api/issues?fault_code=P0217&limit=10"
curl "http://127.0.0.1:9054/api/issues/<JOB_ID>/similar?limit=5"
```

## Manual Escalation Rule

- Set `request_supervisor_review: true` on job intake or workflow step update to force `PENDING_APPROVAL`.
- Backend returns `escalation_reasons` and `risk_signals` so operators can see exactly why a case was routed.
- Escalation policy version is returned as `escalation_policy_version` for traceability.
- Governance policy version is returned as `governance_policy_version`.
- Policy config hash is returned as `policy_config_hash`.
- Risk routing uses a hybrid approach:
  - Ollama semantic classifier when available.
  - Local semantic fallback scoring when Ollama is unavailable, so phrases like “very dangerous” still escalate.
- Safety/warranty keywords, semantic weights, and threshold are tunable in `backend/config/escalation_policy.yaml` (no code edit required).
- Additional enforced governance rules:
  - First-occurrence fault (`equipment_id + fault_code`) auto-escalates.
  - Missing critical parts availability auto-escalates (`parts_unconfirmed`).
  - Pending approvals over 30 minutes auto-transition to `TIMEOUT_HOLD`.
  - Sync failures beyond 3 retries emit `SYNC_FAILURE` supervisor alerts.

## Workflow Modes

- `workflow_mode=INVESTIGATION_ONLY`
  - Used for `PENDING_APPROVAL`, `TIMEOUT_HOLD`, and `DENIED`.
  - Workflow and report provide evidence checklist only.
  - Repair/parts guidance is suppressed (`suppressed_guidance=true`).
- `workflow_mode=FIX_PLAN`
  - Used for `READY`.
  - Workflow and report include actionable repair guidance.

Key response fields for frontend/app behavior:
- `workflow_mode`
- `workflow_intent`
- `allowed_actions`
- `suppressed_guidance`

## Replan Example

```bash
curl -X POST http://127.0.0.1:9054/api/job/<JOB_ID>/replan
```

## Agent Metrics Example

```bash
curl http://127.0.0.1:9054/api/metrics/agent-performance
```

## Ollama and Model Notes

- Backend uses a local open-source model through Ollama via `POST /api/generate`.
- Config lives in `backend/config/ollama_config.yaml`.
- Mode-based routing:
  - Online mode (`OFFLINE=0` and `is_offline=false`) uses `online_model` (default `llama3.1:8b`).
  - Offline mode (`OFFLINE=1` or `is_offline=true`) uses `offline_model` (default `llama3.2:3b`).
- API responses now include:
  - `model_selected`
  - `model_tier` (`online_8b` or `offline_1_3b`)
  - `model_policy_valid` and `model_policy_notes` (for enforcement visibility)
- On-device app fallback behavior when disconnected:
  - Uses local offline fallback model route (`model_tier=offline_1_3b`) with heuristic inference.
  - Returns full job-shaped response immediately and queues original request for reconciliation.
- If Ollama/model is unavailable, backend falls back to deterministic behavior for triage and report generation.
- Use only locally available/open-source models with licenses appropriate for your environment and usage.

Runtime inspection:
```bash
curl "http://127.0.0.1:9054/api/config/runtime?is_offline=false"
curl "http://127.0.0.1:9054/api/config/runtime?is_offline=true"
curl "http://127.0.0.1:9054/api/health?is_offline=true"
```

## Data Provenance Note

- Demo data in this repository is synthetic/public-like placeholder content.
- Do not insert real customer PII or connect this demo backend to real Cummins production systems.
- Synthetic backend datasets now include:
  - `backend/knowledge_base/synthetic/technicians.json`
  - `backend/knowledge_base/synthetic/inventory.json`

## Security Posture (Minimal for Brief)

- Secrets handling: use environment variables; do not commit secrets to the repository.
- Local demo transport: HTTP only for localhost development.
- Production in-transit plan: terminate TLS (HTTPS) behind a reverse proxy/load balancer.
- At-rest plan: restrict SQLite file permissions (for example `chmod 600 backend/local_db/*.db`) and host-level access controls.
