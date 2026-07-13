# Rely Health Take-Home - AI Outbound Calling System - Liam Pinson

## Overview

This is a take-home implementation of an AI outbound calling system for appointment
reminders. An operator opens a web console, picks a patient from a stored list, and
clicks "Call" to trigger a real outbound phone call placed by [Retell](https://retellai.com/)'s
conversational voice AI. The system's job — per the take-home brief — is not the content
of the call itself, but the data model, database, and flow from a stored patient record
through to a tracked, real-world call: durably recording the call attempt before the
provider is ever contacted, deriving call status from a small state machine driven by
Retell webhook deliveries, keeping a raw/untouched log of every webhook event separate
from the system's interpreted belief about a call's current status, and backstopping
that webhook-driven flow with a reconciliation job for the cases webhooks can't cover
(e.g. a dial that never connects at all). The system is intentionally architected for
near-term growth (a second call provider, SMS fallback, etc.) without overbuilding for
requirements that don't exist yet.

## Setup

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose (v2, i.e. `docker
  compose`, not the standalone `docker-compose` binary).
- A [Retell](https://retellai.com/) account with an API key, and at least one phone
  number in your Retell dashboard with an **outbound agent bound to it** (Phone Numbers
  → edit number → set outbound agent). Retell rejects `create-phone-call` with `"No
  outbound agent id set up for phone number."` if this isn't done first.
- [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
  (only needed if you want Retell's webhooks to reach your local backend — see step 4).

### 1. Clone and configure environment variables

```bash
git clone <this-repo-url>
cd AI_Calling_Agent
cp backend/.env.example backend/.env
```

Edit `backend/.env` and fill in your real values:

```
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/ai_calling_agent
RETELL_API_KEY=your_retell_api_key
RETELL_FROM_NUMBER=+15551234567
```

`backend/.env` is the single real secrets file for the backend and is never duplicated
elsewhere in the repo (it's git-ignored). `DATABASE_URL` in this file is
`localhost`-based, which is correct if you ever run the backend natively (outside
Docker); when running via `docker compose` (below), `docker-compose.yml` overrides
`DATABASE_URL` at the container level to point at the `postgres` service hostname
instead, so you don't need to edit it for the Docker path.

### 2. Start the stack

From the repo root:

```bash
docker compose up --build
```

This brings up three services:
- `postgres` (Postgres 16)
- `backend` — runs `alembic upgrade head` (applying all migrations) and then starts
  `uvicorn` on port 8000
- `frontend` — runs `next build` then `next start` on port 3000

Wait until all three report as running/healthy before continuing (backend waits on
Postgres's healthcheck automatically).

### 3. Seed demo patient data

The database starts empty — there's no patient data until you seed it. With the stack
running:

```bash
docker compose exec backend python -m app.seed_patients
```

This inserts a small set (6) of synthetic patients with varied timezones and
appointment dates, for local demo/testing purposes. It's safe to re-run: any patient
whose phone number already exists is skipped (printed as a warning) rather than
duplicated.

(If running the backend natively instead of via Docker, activate the backend's
virtualenv and run `cd backend && python -m app.seed_patients` from the repo root
instead.)

### 4. Expose the backend for Retell webhooks (cloudflared)

Retell delivers call-status webhooks (`call_started`, `call_ended`, `call_analyzed`) to
a URL you configure in the Retell dashboard. Since this is local dev, that URL needs to
tunnel to your machine. A helper script starts the tunnel and prints the URL for you:

```bash
# macOS / Linux / git-bash
scripts/start_tunnel.sh
```
```powershell
# Windows (PowerShell)
scripts/start_tunnel.ps1
```

Run it, then **paste the URL it prints into your Retell dashboard's webhook
configuration** (agent-level or account-level webhook URL field) — that part is still a
manual step you have to do yourself in your browser; a script can't log into your Retell
account for you. The script keeps running in the foreground to keep the tunnel alive;
press Ctrl+C to stop it when you're done.

**Important:** this uses Cloudflare's free *quick tunnel*, which requires zero setup (no
domain, no Cloudflare account) but issues a brand-new random URL every single time it
restarts — there is no persistent/reserved subdomain. A persistent named tunnel would
require owning a domain and tying it to a Cloudflare account, which was judged
unnecessary setup overhead for a take-home project's local dev loop. **Practical
consequence: every time you restart the tunnel, you must go back into the Retell
dashboard and re-paste the new `<url>/events` value** — the script prints a reminder of
this every time — or webhooks will silently stop arriving (they'll just fail to deliver
against the dead old URL).

Both scripts require `cloudflared` to already be installed and on `PATH` (see
Prerequisites above); if it isn't found, they'll tell you so instead of failing
silently.

### 5. Open the app

Once the stack and tunnel are both up, open:

```
http://localhost:3000
```

You'll see the patient list. Click "Call" on any row to place a real outbound call via
Retell and watch its status update live (polled every ~2.5s) through to a terminal
state.

### 6. Run the backend test suite

```bash
cd backend
python -m pytest
```

The suite runs against a dedicated `ai_calling_agent_test` Postgres database (not your
dev/demo database) — it's created automatically on first run against the same Postgres
instance `DATABASE_URL` points at, so Postgres needs to be reachable (e.g. via `docker
compose up postgres` or the full stack) before running tests.

---

## Current Architecture

Everything below is actually built and running today — no speculative pieces. See
"Possible Future Architecture" further down for how this could grow.

```mermaid
flowchart TB
    subgraph Client["Browser"]
        UI["Next.js UI<br/>(patients, Call, status)"]
    end

    subgraph Frontend["Frontend Container"]
        Proxy["Next.js API Routes<br/>(server-side proxy)"]
    end

    subgraph Backend["Backend Container (FastAPI)"]
        Patients["GET /patients"]
        CallEP["POST /patients/{id}/call"]
        Events["POST /events (webhook receiver)"]
        CallsGet["GET /calls/{id}"]
        SM["state_machine.py<br/>(ALLOWED_TRANSITIONS)"]
        Reconcile["reconcile.py<br/>(sweep + BFS replay)"]
        Provider["CallProvider interface"]
        Retell["RetellProvider<br/>(calls + webhooks)"]
    end

    subgraph DB["Postgres"]
        PatientTbl[("patients")]
        CallLogTbl[("call_logs<br/>status, outcome_reason")]
        WebhookTbl[("webhook_events")]
    end

    subgraph External["Retell (external)"]
        RetellAPI["Retell API"]
        Tunnel["cloudflared tunnel<br/>(ephemeral, dev-only)"]
    end

    UI --> Proxy --> Patients & CallEP & CallsGet
    CallEP --> SM --> CallLogTbl
    CallEP --> Provider --> Retell --> RetellAPI
    RetellAPI -.webhook.-> Tunnel --> Events --> WebhookTbl
    Events --> SM
    Reconcile --> Provider
    Reconcile --> SM
    Patients --> PatientTbl
    CallsGet --> CallLogTbl

    class UI,Proxy frontend
    class Patients,CallEP,Events,CallsGet route
    class SM,Reconcile,Provider,Retell logic
    class PatientTbl,CallLogTbl,WebhookTbl db
    class RetellAPI,Tunnel external

    classDef frontend fill:#4A90D9,stroke:#2C5F8A,color:#fff
    classDef route fill:#5CB85C,stroke:#3D8B3D,color:#fff
    classDef logic fill:#F0C040,stroke:#B8901E,color:#000
    classDef db fill:#9E9E9E,stroke:#616161,color:#fff
    classDef external fill:#E8994C,stroke:#B5702A,color:#fff
```

| Color | Meaning |
|---|---|
| Blue | Frontend |
| Green | Backend route/handler |
| Yellow | State machine / business logic |
| Gray | Database table |
| Orange | External service (Retell, including the dev-only cloudflared tunnel) |

### How the pieces fit together

**Provider abstraction.** All Retell-specific logic (API URLs, request/response shapes,
`disconnection_reason` interpretation, error categorization) lives behind a
`CallProvider` interface (`app/providers/base.py`), with `RetellProvider`
(`app/providers/retell.py`) as the sole implementation and `get_provider()`
(`app/providers/factory.py`) selecting it via a `PROVIDER` env var. Route handlers,
`reconcile.py`, and `state_machine.py` depend only on the interface. This is a locked,
non-negotiable boundary: nothing outside `app/providers/` imports `httpx` or references
Retell specifics directly, so a future provider swap (e.g. to Vapi) is scoped to writing
one new file, not touching call-flow logic.

**State machine.** `CallLog.status` moves through five states —
`connecting → dialing → ongoing → closed`, with branches to `connection_failed` (dial
never went through) and `no_response` (rang, nobody picked up) — enforced by an
allow-list of legal transitions in `state_machine.py`, in application code rather than a
DB constraint (this is business logic, not schema). Every transition is derived by the
provider's normalization of a real Retell signal (webhook delivery, sync API response,
or a reconciliation poll) — never copied directly from Retell's own `call_status` field,
which doesn't map 1:1 onto these five states.

**Raw webhook event log vs. derived `CallLog`.** Every webhook delivery is written to
`webhook_events` exactly as received, before any interpretation, keyed on the composite
`(event_type, provider_call_id)` — Retell's own recommended dedup pattern, since it
doesn't provide a single discrete per-event ID. This table is append-only and is the
system's forensic record; `CallLog` (a single mutable row per call, representing the
system's *current belief* about what's happening right now) is derived from it, never
the reverse. Duplicate webhook deliveries are caught by a DB-level unique constraint
(not an app-level existence check), so it's race-safe under concurrent delivery.
Illegal or duplicate transitions are still recorded in `webhook_events` for
auditability, but never applied to `CallLog`.

**Reconciliation.** Webhooks alone can't cover every case — most importantly, if a dial
never connects, Retell never sends `call_started` at all, so there's no webhook path to
`connection_failed`. `app/reconcile.py` is a callable script (`python -m app.reconcile`,
not a scheduled daemon) that sweeps `CallLog` for rows stuck in a non-terminal state past
a threshold, polls Retell's `GET /v2/get-call/{call_id}` directly for ground truth
(bypassing webhooks entirely), and applies the resulting transition via a shortest-path
walk through `state_machine.py`'s **unmodified** transition graph — replaying any
missing intermediate hop as a sequence of individually legal transitions, rather than
allowing an illegal direct jump. This is safe specifically because reconciliation has
direct positive evidence from the provider that the intermediate state actually
occurred, unlike a webhook, which only ever witnesses one transition at a time.

---

## Data Model

Three tables, pulled directly from `backend/app/models.py`.

### `patients`

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `id` | UUID | No (PK) | Primary key, generated client-side via `uuid4()` |
| `first_name` | String | No | Patient's first name |
| `last_name` | String | No | Patient's last name |
| `date_of_birth` | Date | No | Patient's date of birth |
| `phone_number` | String | No | E.164 number Retell dials |
| `appointment_date` | Date | No | Date of the upcoming appointment being reminded about |
| `appointment_time` | Time | No | Time of the upcoming appointment |
| `timezone` | String | No | IANA timezone string for the appointment (e.g. `America/New_York`) |

`insurance_number`, `medical_record_number`, and `home_address` are deliberately not
columns here — see "What's Left Undone."

### `call_logs`

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `call_id` | UUID | No (PK) | Our own call identifier, generated at click-time — before Retell ever responds |
| `patient_id` | UUID | No (FK → `patients.id`) | Which patient this call was placed to |
| `provider_call_id` | String | Yes (unique) | Retell's `call_id`, filled in once the sync API responds; `null` if the provider call never got that far |
| `status` | String | No | Current state-machine status (`connecting`/`dialing`/`ongoing`/`closed`/`connection_failed`/`no_response`) |
| `started_at` | DateTime (tz-aware) | No | When this row was written, before Retell was contacted |
| `ended_at` | DateTime (tz-aware) | Yes | When the call reached a terminal state, from the webhook/poll's own `event_timestamp` |
| `error_reason` | String | Yes | Free-text detail when a provider call fails (e.g. the HTTP status line) |
| `outcome_reason` | String | Yes | Shorthand terminal-state annotation (raw `disconnection_reason`, an upgraded voicemail label, or a `ProviderCallError` category) — stored alongside `status`, never used to derive it |

`provider_call_id` has a DB-level unique constraint (`uq_call_logs_provider_call_id`).

### `webhook_events`

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `event_type` | String | No (composite PK) | Retell's event name (`call_started` / `call_ended` / `call_analyzed`) |
| `provider_call_id` | String | No (composite PK) | Retell's `call_id` — combined with `event_type` as the dedup key |
| `raw_payload` | JSONB | No | The full webhook body exactly as received, untouched |
| `received_at` | DateTime (tz-aware) | No | When this server received the delivery |

---

## API Endpoints

Real example payloads below are pulled from
`postman/Rely_Health_Take_Home.postman_collection.json`, captured against a live run of
this system — not invented.

### `GET /patients`

List every patient for the operator to pick a call target from. No auth, no pagination.

**Request:** no body, no params.

**Response — `200`:**
```json
[
  {
    "id": "bc7c4930-4924-4603-ae87-92e8cfc1b7fc",
    "first_name": "Maria",
    "last_name": "Gonzalez",
    "date_of_birth": "1985-03-12",
    "phone_number": "+15555550101",
    "appointment_date": "2026-07-11",
    "appointment_time": "09:00:00",
    "timezone": "America/New_York"
  }
]
```

**Status codes:** `200` always (empty array if nothing's been seeded yet).

### `POST /patients/{patient_id}/call`

Places an outbound call for the given patient — writes the `CallLog` row before
contacting Retell (see "Request Flow" below for why).

**Request:** no body; `patient_id` path param.

**Response — `201`** (always 201, even on a provider failure — the body's `status`
field is what tells you what happened):
```json
{
  "call_id": "6041129a-3ae6-455c-b551-743ce70bbf26",
  "patient_id": "bc7c4930-4924-4603-ae87-92e8cfc1b7fc",
  "provider_call_id": null,
  "status": "connection_failed",
  "started_at": "2026-07-12T22:02:21.452570Z",
  "ended_at": null,
  "error_reason": "Client error '400 Bad Request' for url 'https://api.retellai.com/v2/create-phone-call'",
  "outcome_reason": "invalid_request"
}
```

**Response — `404`** (unknown `patient_id`):
```json
{ "detail": "Patient not found" }
```

**Status codes:** `201` (row created regardless of provider outcome), `404` (no such
patient).

### `GET /calls/{call_id}`

Poll a call's current status — this is what the frontend hits every ~2.5s until a
terminal state.

**Request:** no body; `call_id` path param.

**Response — `200`:**
```json
{
  "call_id": "841098b8-36f8-44c9-b9c2-0e9d62ddd663",
  "patient_id": "5b91b126-f25f-4700-a0a2-eb0eaed0d0e2",
  "provider_call_id": "call_8d5921decbb01d6490cded971d6",
  "status": "closed",
  "started_at": "2026-07-12T18:47:31.186532Z",
  "ended_at": "2026-07-12T18:48:15.164000Z",
  "error_reason": null,
  "outcome_reason": "voicemail (detected late)"
}
```

**Response — `404`:**
```json
{ "detail": "Call not found" }
```

**Status codes:** `200`, `404`.

### `POST /events`

Retell's webhook receiver — `call_started`, `call_ended`, and `call_analyzed` all land
here. See "Request Flow" below for the full pipeline.

**Request** (example — `call_ended`):
```json
{
  "event": "call_ended",
  "call": {
    "call_id": "demo-user-hangup",
    "disconnection_reason": "user_hangup"
  },
  "event_timestamp": 1700000010000
}
```

**Response — `200`**, one of three bodies depending on what happened:
- `{"status": "applied"}` — legal transition, `CallLog` updated
- `{"status": "recorded"}` — logged to `webhook_events` but not applied (illegal/duplicate
  transition, or an informational-only event like `call_analyzed`)
- `{"status": "duplicate_ignored"}` — this exact `(event_type, provider_call_id)` pair
  was already delivered once

**Status codes:** `200` always. No signature verification is implemented yet (see
"What's Left Undone"), so the endpoint doesn't reject unauthenticated payloads at the
HTTP level — it just may not apply them.

---

## Request Flow: What Happens When You Click "Call"

1. Operator clicks "Call" next to a patient row in the Next.js UI (`PatientRow.tsx`).
2. The browser POSTs to the frontend's own `/api/patients/{id}/call` **Route Handler** —
   never directly to the backend (this is the CORS workaround, not a security boundary).
3. That Route Handler proxies server-side to the real backend:
   `POST {API_BASE_URL}/patients/{id}/call`.
4. The backend looks up the patient; if not found, `404` and nothing is written.
5. **The DB write happens first, deliberately, before Retell is ever contacted:** a new
   `CallLog` row is inserted with `status: "connecting"` and committed. This is the
   load-bearing sequencing decision in this project — if the write happened *after* the
   provider call, a successful Retell call paired with a failed DB write would be
   invisible forever (unrecoverable). Writing first means a failed provider call after a
   successful write is merely detectable and recoverable instead.
6. The backend calls `CallProvider.place_call()` → `RetellProvider` → Retell's
   `POST /v2/create-phone-call`.
   - **On failure:** `CallLog.status` → `connection_failed`, `error_reason`/
     `outcome_reason` populated from the categorized `ProviderCallError` — the row is
     never lost, just marked failed.
   - **On success:** Retell's sync response includes a real `call_id` immediately, so
     `CallLog.provider_call_id` is populated right away and `status` → `dialing`.
7. The `201` response flows back through the frontend proxy to the browser, which starts
   polling `GET /calls/{call_id}` every ~2.5s.
8. Retell places the real phone call. As it progresses, Retell delivers webhooks to the
   tunnel URL configured in the dashboard: `call_started` (`dialing` → `ongoing`), then
   `call_ended` (`ongoing` → `closed`/`no_response`, depending on `disconnection_reason`),
   and separately `call_analyzed` (informational, may upgrade `outcome_reason`).
9. Each webhook delivery is inserted into `webhook_events` first, unconditionally
   (append-only, keyed on `(event_type, provider_call_id)` for dedup) — before any
   interpretation happens.
10. The normalized event is checked against `state_machine.py`'s `ALLOWED_TRANSITIONS`;
    a legal transition updates `CallLog.status` (and `outcome_reason`/`ended_at` where
    relevant), an illegal or duplicate one is recorded but not applied.
11. The frontend's next poll of `GET /calls/{call_id}` picks up the new status; once it's
    one of the three terminal states (`closed`, `connection_failed`, `no_response`),
    polling stops and the UI shows the final "status · reason" label.

---

## Possible Future Architecture

**Speculative — none of the purple components below are implemented today. This shows
one possible direction, not a commitment.**

```mermaid
flowchart TB
    subgraph Client["Browser"]
        UI["Next.js UI<br/>(patients, Call, status)"]
    end

    subgraph Frontend["Frontend Container"]
        Proxy["Next.js API Routes<br/>(server-side proxy)"]
    end

    subgraph Backend["Backend Container (FastAPI)"]
        Patients["GET /patients"]
        CallEP["POST /patients/{id}/call"]
        Events["POST /events (webhook receiver)"]
        CallsGet["GET /calls/{id}"]
        SM["state_machine.py<br/>(ALLOWED_TRANSITIONS)"]
        Reconcile["reconcile.py<br/>(sweep + BFS replay)"]
        Provider["CallProvider interface"]
        Retell["RetellProvider<br/>(calls + webhooks)"]
    end

    subgraph DB["Postgres"]
        PatientTbl[("patients")]
        CallLogTbl[("call_logs")]
        WebhookTbl[("webhook_events")]
    end

    subgraph External["Retell (external)"]
        RetellAPI["Retell API"]
        Tunnel["cloudflared tunnel<br/>(dev-only)"]
    end

    UI --> Proxy --> Patients & CallEP & CallsGet
    CallEP --> SM --> CallLogTbl
    CallEP --> Provider --> Retell --> RetellAPI
    RetellAPI -.webhook.-> Tunnel --> Events --> WebhookTbl
    Events --> SM
    Reconcile --> Provider
    Reconcile --> SM
    Patients --> PatientTbl
    CallsGet --> CallLogTbl

    Vapi["VapiProvider<br/>(2nd provider)"]
    SMS["SMS / multi-channel<br/>fallback"]
    Escalation["Escalation to<br/>human navigator"]
    Batch["Batch / bulk<br/>calling orchestrator"]
    EHR["EHR sync service"]
    Scheduler["Cron / APScheduler<br/>(auto-run reconcile.py)"]
    WS["WebSocket push<br/>(replace polling)"]
    Auth["Auth layer<br/>(session/token, RBAC)"]

    Provider -.-> Vapi
    CallLogTbl -.-> SMS
    SM -.-> Escalation
    CallEP -.-> Batch
    PatientTbl -.-> EHR
    Reconcile -.-> Scheduler
    CallsGet -.-> WS
    Client -.-> Auth
    Backend -.-> Auth

    class UI,Proxy frontend
    class Patients,CallEP,Events,CallsGet route
    class SM,Reconcile,Provider,Retell logic
    class PatientTbl,CallLogTbl,WebhookTbl db
    class RetellAPI,Tunnel external
    class Vapi,SMS,Escalation,Batch,EHR,Scheduler,WS,Auth future

    classDef frontend fill:#4A90D9,stroke:#2C5F8A,color:#fff
    classDef route fill:#5CB85C,stroke:#3D8B3D,color:#fff
    classDef logic fill:#F0C040,stroke:#B8901E,color:#000
    classDef db fill:#9E9E9E,stroke:#616161,color:#fff
    classDef external fill:#E8994C,stroke:#B5702A,color:#fff
    classDef future fill:#C9A0DC,stroke:#7B4397,color:#000,stroke-dasharray: 5 5
```

| Color | Meaning |
|---|---|
| Blue | Frontend |
| Green | Backend route/handler |
| Yellow | State machine / business logic |
| Gray | Database table |
| Orange | External service (Retell) |
| Purple (dashed border) | Not implemented — hypothetical future component |

- **Second provider (e.g. Vapi):** implement `CallProvider` in a new
  `app/providers/vapi.py` and register it in `factory.py` — no changes needed to route
  handlers, `reconcile.py`, or `state_machine.py`; that boundary was built for exactly
  this.
- **SMS fallback:** on a `no_response`/`connection_failed` terminal state, trigger an SMS
  send via a new provider-style abstraction (e.g. `SmsProvider`) — reuses the same
  "write intent to DB before calling an external API" sequencing already used for calls.
- **Escalation to a human navigator:** a new terminal/queued state plus a notification
  hook (Slack/email) fired on `no_response`/`connection_failed` — mostly a
  `state_machine.py` and routing addition, not a new subsystem.
- **Batch calling:** a scheduler that iterates upcoming appointments and calls
  `POST /patients/{id}/call` for each — the existing sequencing/idempotency guarantees
  already support this; the new work is purely orchestration.
- **EHR sync:** a periodic or event-driven job to pull `Patient` fields (and reintroduce
  fields dropped today, like `insurance_number`) from a real EHR — `patients` becomes a
  projection/cache rather than the source of truth.
- **Retry/backoff:** wrap `place_call()` failures in a retry policy before falling back
  to `connection_failed` — contained entirely within `calls.py`'s provider-call boundary,
  doesn't touch the state machine.
- **WebSockets/real-time push:** replace or supplement the ~2.5s polling loop with a
  push channel — additive to the existing polling fallback for resilience, not a
  replacement.
- **Auth:** add an API-gateway/auth layer in front of all four routes, plus wire up
  Retell's `Retell.verify()` on `POST /events` so the webhook receiver stops trusting
  arbitrary payloads — the single highest-priority item here given the system handles
  real (if fictional/test) phone numbers.

---

## Design Notes & Known Tradeoffs

### Test fixture naming

**Note on test data:** `CallLog` rows with `provider_call_id` values prefixed
`cat-refactor-*` or `vm-regr-*` are synthetic fixtures created while verifying
the provider error-categorization change and the voicemail-mapping fix,
respectively. They are not real call attempts and are left in place
intentionally as a comparison baseline against real Retell-driven
transitions, not cleaned up as leftover test cruft.

### `seed_patients.py`: dev fixture, not production tooling

Standalone script (`python -m app.seed_patients`), never wired into app startup — exists
purely so a fresh clone has realistic demo data to click through (varied
timezones/appointment dates, `+1555`-prefixed fictional numbers). Idempotent-safe (skips
rows whose `phone_number` already exists) and deliberately kept separate from any real
patient data used in live Retell testing, so demo fixtures and real test calls never mix.

### cloudflared: two caveats worth knowing

1. **No webhook signature verification.** `POST /events` trusts any payload sent to it —
   Retell's `Retell.verify()` helper (checks `X-Retell-Signature`) isn't wired up.
   Combined with a public tunnel URL, anyone who discovers it while live could POST a
   fabricated webhook. Fine for a local, ephemeral, hard-to-guess URL; not fine beyond
   that (see "What's Left Undone").
2. **Fresh URL on every restart.** The single most common "why did my webhooks stop
   arriving" gotcha during development — restarting `cloudflared` silently orphans the
   old URL, and Retell keeps retrying against it until you update the dashboard.

### Voicemail mapping: what's covered, what isn't

Only three `disconnection_reason` values are mapped explicitly — `user_hangup`/
`agent_hangup` → `closed`, `dial_no_answer`/`voicemail_reached` → `no_response` —
everything else defaults to `closed` (named non-goal, not an oversight). `voicemail_reached`
(real-time, on `call_ended`) is a different signal from `call_analysis.in_voicemail`
(retrospective, on `call_analyzed`), and they can genuinely diverge: a call where the
agent talked through voicemail and hung up itself surfaces `disconnection_reason:
"agent_hangup"` at `call_ended` (→ `closed`), with `call_analysis.in_voicemail: true`
only showing up later on the separate `call_analyzed` event. See `outcome_reason` below
for how that divergence is now surfaced instead of silently lost.

### `outcome_reason`: a shorthand annotation next to every terminal status

`CallLog.outcome_reason` (nullable) sits alongside `status` — never used to *derive* it.
At `call_ended` it's set to the raw `disconnection_reason`; at `call_analyzed`, if
`call_analysis.in_voicemail` is `true` and the row isn't already voicemail-confirmed,
it's upgraded to `"voicemail (detected late)"` — so a call that resolved `closed` via
`agent_hangup` but was later confirmed as voicemail is visibly distinguishable, instead
of that distinction staying buried in `call_analyzed`'s informational-only payload.
`connection_failed` rows get `outcome_reason` from `ProviderCallError.category`
(`invalid_request`/`provider_config_error`/`unknown`). The frontend maps these to short
labels ("closed · voicemail", "no response · no answer", etc.) instead of raw enum
strings. Verified live against `call_id=841098b8-...` and — after a real
`call_ended`/`call_analyzed` ordering bug was found and fixed (see `CLAUDE.md`) — against
`call_id=call_7300eee9...` as well.

**Note on voicemail-label precedence:** `outcome_reason` preserves whichever
voicemail confirmation arrives first (real-time `voicemail_reached` or
retrospective `call_analysis.in_voicemail` via `call_analyzed`), regardless
of arrival order, to prevent either from clobbering the other with a less
informative value. In the rare case where the retrospective signal arrives
first, a later real-time signal won't upgrade the label to the more
specific one — both values correctly indicate voicemail either way, so
this is a scoped-out cosmetic refinement, not a correctness gap.

### Unexpected Retell-agent SMS behavior

**Note on unexpected SMS behavior:** The Retell test agent appears to have
some default post-call SMS behavior on voicemail detection, configured at
the agent/platform level. This is not something built by this system, is
not tracked in `CallLog`/webhook data, and is outside this project's scope.
Flagged here so it isn't mistaken for a feature of this application during
a live walkthrough.

### Future test coverage, by extension point

The current suite (44 tests) covers the system as built today. Each extension in
"Possible Future Architecture" above would need its own dedicated coverage:
- **Second provider:** a parallel `test_vapi_provider.py` mirroring
  `test_retell_provider.py`'s shape, plus a factory test confirming `PROVIDER=vapi`
  actually selects it.
- **SMS fallback:** a `no_response`/`connection_failed` state triggers exactly one SMS
  attempt, and a send failure doesn't corrupt `CallLog` (same pattern as the existing
  "provider failure leaves the row persisted" test).
- **Escalation:** idempotency around whatever new terminal/queued state gets added
  (doesn't double-escalate on a duplicate webhook).
- **Batch calling:** concurrency tests confirming DB-write-before-provider-call holds
  under many simultaneous `POST /patients/{id}/call` calls.
- **EHR sync:** contract tests against the sync boundary, plus confirming PHI fields
  dropped today still aren't persisted if/when they start flowing through.
- **Retry/backoff:** a transient failure retries up to N times before `connection_failed`,
  without creating duplicate `CallLog` rows.
- **WebSockets:** a status change is pushed to a connected client, and a disconnected
  client falls back to/reconciles against a normal poll.
- **Auth:** unauthenticated requests to every route are rejected, and `POST /events`
  rejects an invalid/missing `X-Retell-Signature` once implemented.

---

## What's Left Undone

Per the brief's explicit request for an honest list of what wasn't built — these are
all deliberate scope decisions, not oversights, and are covered in more detail above and
inline in the code:

- **No full immutable/append-only audit trail.** `CallLog` is a single mutable row per
  call, not an append-only ledger. `webhook_events` is append-only and can reconstruct
  history, but there's no dedicated audit-log abstraction on top of it.
- **No auth or API gateway anywhere**, including no webhook signature verification on
  `POST /events` (Retell's `Retell.verify()` helper is not wired up). The Next.js proxy
  layer is a CORS workaround, not a security boundary.
- **No WebSockets/real-time push** — the frontend polls `GET /calls/{id}` every ~2.5s
  instead.
- **No scalable/async webhook processing** — `POST /events` is handled synchronously,
  no queue. Fine at this scale; would need revisiting specifically if the synchronous
  write path risks Retell's 10-second webhook delivery timeout.
- **Reconciliation is a callable script, not a scheduled job** — `python -m
  app.reconcile` has to be run manually (or via cron/systemd-timer in a real
  deployment, which isn't wired up here).
- **Reconciliation's stuck-threshold (15 minutes) is a placeholder**, not yet informed
  by precisely measured real-world Retell call-setup latency.
- **`disconnection_reason` mapping is partial** — only `user_hangup`, `agent_hangup`,
  `dial_no_answer`, and `voicemail_reached` are handled explicitly; everything else
  defaults to `closed`.
- **`call_analysis.in_voicemail` isn't correlated back into `CallLog.status`** — it's
  used to upgrade the informational `outcome_reason` field (see above) but never
  changes what state a call is considered to be in.
- **No unused-PHI-field EHR sync.** `insurance_number`, `home_address`, and
  `medical_record_number` were deliberately dropped from `Patient` (least-privilege /
  PHI minimization) rather than stored and left unused; a real deployment would sync
  these from an EHR rather than duplicating them here.

---

A light visual theme (font, color palette, spacing) was applied loosely referencing
Rely Health's own site — functionality and structure were prioritized over
pixel-perfect design fidelity, consistent with the take-home's "don't spend the whole
project on polish" guidance.
