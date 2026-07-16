// The browser connects to the backend's WebSocket endpoint directly --
// Next.js Route Handlers (app/api/**) can't proxy a WebSocket upgrade, so
// the "browser never talks to the backend directly" rule that API_BASE_URL
// exists for (see lib/config.ts) doesn't extend to this one channel.
// WebSocket connections aren't subject to the fetch/CORS restrictions that
// rule was written to sidestep, so no backend CORS support is needed.
//
// Port 8000 mirrors docker-compose.yml's published backend port -- update
// both together if that ever changes. Deliberately not a NEXT_PUBLIC_ env
// var: that would need build-time ARG plumbing through frontend/Dockerfile
// and docker-compose.yml for one hardcoded local-dev constant, which isn't
// worth the added complexity at this project's scope.
const BACKEND_WS_PORT = 8000;

export function transcriptFeedUrl(callId: string): string {
  return `ws://${window.location.hostname}:${BACKEND_WS_PORT}/calls/${callId}/transcript-feed`;
}
