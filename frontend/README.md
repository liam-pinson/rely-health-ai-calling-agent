# AI Calling Agent -- Frontend

Thin operator console for the take-home: list patients, place an outbound
appointment-reminder call, and watch its status resolve. Deliberately
minimal -- functional demo, not a design showcase.

## Setup

```bash
npm install
cp .env.local.example .env.local   # defaults to http://localhost:8000
npm run dev
```

Open http://localhost:3000. The backend (FastAPI + Postgres, see
`../backend/`) must be running for anything beyond the empty page to work.

## What's here

- `app/page.tsx` -- patient list (fetched on mount, retry on failure)
- `components/PatientTable.tsx` / `PatientRow.tsx` -- one row per patient,
  each with its own "Call" button and independent poll loop
- `app/api/**/route.ts` -- server-side proxy routes; see "Architecture note"
- `lib/config.ts` -- single source of truth for the backend base URL
  (`API_BASE_URL`)
- `lib/types.ts` -- `Patient` / `CallLog` shapes matching the backend's
  response bodies, plus the terminal-status list used to stop polling
- `lib/errors.ts`, `lib/proxy.ts` -- small shared helpers, not
  frameworks-in-waiting

## Architecture note: why a server-side proxy

`API_BASE_URL` is read only inside the `app/api/**/route.ts`
Route Handlers, which do the actual server-to-server fetch to the FastAPI
backend. The browser only ever calls this frontend's own `/api/*` paths,
same-origin. It's deliberately not prefixed `NEXT_PUBLIC_` -- that prefix
inlines the value into the JS bundle at `next build` time, which would
freeze it into a Docker image and ignore any `docker-compose` runtime
override (see `docker-compose.yml` at the repo root: the frontend
container gets `API_BASE_URL=http://backend:8000`, the internal Docker
network hostname, set at container start, not baked in at build time).

This wasn't in the original spec and isn't a backend change, so I made the
call myself rather than stopping: the backend has no CORS middleware
configured, and modifying `backend/` was out of scope for this task. A
direct browser-to-backend fetch would be blocked by the browser's CORS
policy the moment frontend and backend run on different ports (which they
do in local dev -- 3000 vs 8000). Proxying server-side sidesteps that
entirely without touching the backend or changing its API contract in any
way -- every request/response shape is passed through unmodified.

If you'd rather the browser call the backend directly, the backend needs
`CORSMiddleware` added (a one-line change in `backend/app/main.py`), and
the `fetch()` calls in `components/PatientRow.tsx` / `app/page.tsx` would
target `${API_BASE_URL}/...` directly instead of `/api/...`.

## Call status states

`connecting`, `dialing`, `ongoing` are in-progress -- polling continues.
`closed`, `connection_failed`, `no_response` are terminal -- polling stops
(see `lib/types.ts:isTerminalStatus`).
