// Single source of truth for the backend base URL. Only read on the
// server (in Route Handlers under app/api/**) -- the browser never talks
// to the backend directly, which sidesteps needing CORS support on it.
//
// Deliberately NOT prefixed with NEXT_PUBLIC_: that prefix tells Next.js
// to inline the value into the JS bundle at `next build` time, which
// would freeze it permanently into a Docker image and ignore any
// docker-compose environment override at container start. A plain
// server-only var is read fresh from process.env at request time.
export const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";
