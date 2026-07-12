# Rely Health Take-Home — AI Outbound Calling System

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

## Architecture Diagram

```mermaid
flowchart TB
    subgraph Client["Browser"]
        UI["Next.js UI<br/>(patient list, Call button, status)"]
    end

    subgraph Frontend["Frontend Container"]
        Proxy["Next.js API Routes<br/>(server-side proxy, avoids CORS)"]
    end

    subgraph Backend["Backend Container (FastAPI)"]
        Patients["GET /patients"]
        CallEP["POST /patients/{id}/call"]
        Events["POST /events (webhook receiver)"]
        CallsGet["GET /calls/{id}"]
        SM["state_machine.py<br/>(ALLOWED_TRANSITIONS)"]
        Reconcile["reconcile.py<br/>(manual sweep, BFS hop-replay)"]
        Provider["CallProvider interface"]
        Retell["RetellProvider<br/>(place_call, parse_webhook_event,<br/>get_call_status, error categorization)"]
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

    Vapi["VapiProvider<br/>(2nd provider)"]:::ext
    SMS["SMS / multi-channel<br/>fallback"]:::ext
    Escalation["Escalation to<br/>human navigator"]:::ext
    Batch["Batch / bulk<br/>calling orchestrator"]:::ext
    EHR["EHR sync service"]:::ext
    Scheduler["Cron / APScheduler<br/>(auto-run reconcile.py)"]:::ext
    WS["WebSocket push<br/>(replace polling)"]:::ext
    Auth["Auth layer<br/>(session/token, RBAC)"]:::ext

    Provider -.-> Vapi
    CallLogTbl -.-> SMS
    SM -.-> Escalation
    CallEP -.-> Batch
    PatientTbl -.-> EHR
    Reconcile -.-> Scheduler
    CallsGet -.-> WS
    Client -.-> Auth
    Backend -.-> Auth

    classDef ext stroke-dasharray: 5 5,fill:none;
```

---

## Architecture & Rationale

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

## Design Notes & Known Tradeoffs

### Test fixture naming

**Note on test data:** `CallLog` rows with `provider_call_id` values prefixed
`cat-refactor-*` or `vm-regr-*` are synthetic fixtures created while verifying
the provider error-categorization change and the voicemail-mapping fix,
respectively. They are not real call attempts and are left in place
intentionally as a comparison baseline against real Retell-driven
transitions, not cleaned up as leftover test cruft.

### `seed_patients.py`: dev fixture, not production tooling

`seed_patients.py` is a standalone script (`python -m app.seed_patients`), never wired
into app startup. It exists purely so a fresh clone has plausible demo data to click
through in the UI — realistic name/timezone/appointment-date variety, `+1555`-prefixed
fictional-use phone numbers. It's idempotent-safe (skips any row whose phone number
already exists rather than duplicating it), so it's safe to re-run after a partial seed
or a restart. It's deliberately kept separate from any real patient data inserted for
live end-to-end testing against the real Retell API, so demo fixtures and real test
calls never get tangled together in the same rows.

### cloudflared: two caveats worth knowing before you rely on it

1. **No webhook signature verification.** Retell ships a `Retell.verify()` helper that
   checks the `X-Retell-Signature` header against your API key to confirm a webhook
   really came from Retell. This is **not implemented** — `POST /events` currently
   trusts any payload sent to it. Combined with a public `trycloudflare.com` URL, this
   means anyone who discovers the tunnel URL while it's live could POST a fabricated
   webhook. Acceptable for local dev on an ephemeral, hard-to-guess URL; not acceptable
   as-is for anything beyond that (see "What's Left Undone" below).
2. **Fresh URL on every restart.** Covered in Setup step 4 above — worth repeating here
   because it's the single most common "why did my webhooks stop arriving" gotcha
   during development: restarting `cloudflared` silently orphans the old URL, and Retell
   will keep trying to deliver to it until you update the dashboard.

### Voicemail mapping: what's covered, what isn't

Only three `disconnection_reason` values are mapped explicitly: `user_hangup` and
`agent_hangup` → `closed`; `dial_no_answer` and `voicemail_reached` → `no_response`.
Every other/unrecognized `disconnection_reason` value defaults to `closed`. This is a
named non-goal, not an oversight — full enumeration of Retell's `disconnection_reason`
space was out of scope for this build. Separately, `voicemail_reached` (a *real-time*
signal on `call_ended`) is distinct from `call_analysis.in_voicemail` (a *retrospective*,
transcript-derived signal that only arrives later on `call_analyzed`) — confirmed via
live testing that these two can genuinely diverge: a call where the agent talked through
voicemail and hung up itself surfaces `disconnection_reason: "agent_hangup"` (mapped to
`closed`, not `no_response`) at `call_ended`, with `call_analysis.in_voicemail: true`
only showing up afterward on the separate `call_analyzed` event. See `outcome_reason`
below for how that divergence is now surfaced to the operator instead of silently lost.

### `outcome_reason`: a shorthand annotation next to every terminal status

`CallLog` has a nullable `outcome_reason` column, populated alongside `status` — never
used to *derive* it, so all existing state-machine/mapping logic is untouched. At
`call_ended`, it's set to the raw `disconnection_reason` value from the webhook payload
(`user_hangup`, `agent_hangup`, `dial_no_answer`, `voicemail_reached`). At `call_analyzed`
(still a no-op for `status`), if `call_analysis.in_voicemail` is `true` and
`outcome_reason` doesn't already say `voicemail_reached`, it's upgraded to `"voicemail
(detected late)"` — so a call that resolved to `closed` via `agent_hangup` but was later
confirmed by transcript analysis to actually be voicemail is visibly distinguishable in
the UI from a `closed` that was a genuine hangup, instead of that distinction being
buried in `call_analyzed`'s informational-only payload. `connection_failed` rows also get
an `outcome_reason`, populated from `ProviderCallError.category` (`invalid_request` /
`provider_config_error` / `unknown`) at the point the provider call fails. The frontend
maps these raw values to short human-readable labels (e.g. "closed · voicemail",
"no response · no answer", "connection failed · provider config error") rather than
showing raw enum strings. Verified live against a real call
(`call_id=841098b8-36f8-44c9-b9c2-0e9d62ddd663`) that hit exactly this divergence: its
`disconnection_reason` was `agent_hangup` (→ `closed`) but `call_analysis.in_voicemail`
was `true`, confirming the "closed · voicemail" label now surfaces correctly where it
previously would have just read "closed" with no indication anything was off.

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

The current suite (43 tests) covers the system as built today. Each likely extension
below would need its own dedicated coverage before shipping:
- **Second provider (e.g. Vapi):** a parallel `test_vapi_provider.py` mirroring
  `test_retell_provider.py`'s shape (webhook-mapping table + `respx`-mocked
  `place_call` categorization), plus a factory test confirming `PROVIDER=vapi` actually
  selects it.
- **SMS fallback:** tests that a `no_response`/`connection_failed` terminal state
  triggers exactly one SMS send attempt, and that a send failure doesn't corrupt
  `CallLog` (mirroring the existing "provider failure leaves the row persisted" test
  pattern).
- **Escalation to a human navigator:** tests around whatever new terminal/queued state
  gets added, plus that escalation is idempotent (doesn't double-escalate on a
  duplicate webhook, same DB-level dedup pattern as today).
- **Batch calling:** concurrency tests — many simultaneous `POST /patients/{id}/call`
  calls against the same patient, confirming the DB-write-before-provider-call
  sequencing holds under load and no call gets silently dropped.
- **EHR sync:** contract tests against whatever sync boundary is introduced (e.g. a
  scheduled pull job), plus tests that PHI fields dropped today
  (`insurance_number`, `medical_record_number`) still aren't persisted if/when they
  start flowing through that sync.
- **Retry/backoff:** tests that a transient provider failure is retried up to N times
  with backoff before finally being marked `connection_failed`, and that retries don't
  create duplicate `CallLog` rows.
- **WebSockets:** tests that a status change is pushed to a connected client, and that a
  disconnected/reconnecting client falls back to (or reconciles against) a normal
  `GET /calls/{id}` poll.
- **Auth:** tests that unauthenticated requests to every route are rejected, and
  (separately) that `POST /events` rejects payloads with an invalid/missing
  `X-Retell-Signature` once signature verification is implemented.

---

## Likely Extensions

- **Second provider (e.g. Vapi):** implement `CallProvider` in a new
  `app/providers/vapi.py` (place_call/parse_webhook_event/get_call_status,
  Vapi-specific error categorization) and register it in `factory.py` — no changes
  needed to route handlers, `reconcile.py`, or `state_machine.py`, since that boundary
  was built for exactly this.
- **SMS fallback:** on a `no_response`/`connection_failed` terminal state, trigger an
  SMS send (a new small provider-style abstraction, e.g. `SmsProvider`) — reuses the
  same "write intent to DB before calling an external API" sequencing decision already
  used for phone calls.
- **Escalation to a human navigator:** a new terminal/queued state plus a notification
  hook (e.g. Slack/email) fired when a call lands in `no_response` or
  `connection_failed`, so a human can follow up — mostly a `state_machine.py` and
  routing addition, not a new subsystem.
- **Batch calling:** a scheduler that iterates patients with upcoming appointments and
  calls `POST /patients/{id}/call` for each — the existing sequencing/idempotency
  guarantees already support this; the new work is purely the scheduling/orchestration
  layer.
- **EHR sync:** a periodic or event-driven job to pull `Patient` fields (and
  reintroduce fields deliberately dropped today, like `insurance_number`) from a real
  EHR system instead of this repo owning that data — the `Patient` table's schema
  would become a projection/cache rather than the source of truth.
- **Retry/backoff:** wrap `place_call()` failures in a retry policy (e.g. exponential
  backoff, capped attempts) before falling back to `connection_failed` — contained
  entirely within the provider-call boundary in `calls.py`, doesn't touch the state
  machine.
- **WebSockets/real-time push:** replace or supplement the frontend's ~2.5s polling
  loop with a push channel from backend to browser on every `CallLog` status change —
  additive to the existing polling fallback rather than a replacement, for resilience.
- **Auth:** add an API-gateway/auth layer in front of all four routes, plus wire up
  Retell's `Retell.verify()` against `X-Retell-Signature` on `POST /events` so the
  webhook receiver stops trusting arbitrary payloads — the single highest-priority item
  in this list given the system currently handles real (if fictional/test) phone
  numbers.

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