// Thin server-side passthrough to the backend. The browser only ever
// talks to our own /api/* routes; this is what actually calls the
// FastAPI backend, server-to-server, so no backend CORS support is
// needed for this frontend to work.
export async function proxyJson(
  url: string,
  init?: RequestInit
): Promise<Response> {
  try {
    const res = await fetch(url, { ...init, cache: "no-store" });
    const body = await res.text();
    return new Response(body, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return Response.json(
      { detail: "Could not reach the backend API." },
      { status: 502 }
    );
  }
}
