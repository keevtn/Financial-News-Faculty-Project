import { NextResponse } from "next/server";

// Server-side proxy for the key-protected catalyst "Run now" trigger.
//
// The browser POSTs here (same-origin); this handler adds the secret X-API-Key
// and forwards to the FastAPI middleware, so AGENT_API_KEY never ships to the
// client bundle. Runs on the Node runtime because it reads server-only env vars.
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// Server-only base URL (NOT NEXT_PUBLIC). Falls back to the public var / localhost
// so local dev works without extra config.
const API_BASE =
  process.env.API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// Catalyst lanes accepted from the browser; anything else falls back to combined
// so a tampered query string can't reach an unintended backend code path.
const ALLOWED_PROFILES = new Set(["combined", "regulatory"]);

export async function POST(req: Request) {
  const apiKey = process.env.AGENT_API_KEY;
  if (!apiKey) {
    return NextResponse.json(
      { error: "Server is missing AGENT_API_KEY — manual run is disabled." },
      { status: 503 },
    );
  }

  const requested = new URL(req.url).searchParams.get("profile") ?? "combined";
  const profile = ALLOWED_PROFILES.has(requested) ? requested : "combined";

  let upstream: Response;
  try {
    upstream = await fetch(
      `${API_BASE}/api/catalyst/run?trigger=manual&profile=${encodeURIComponent(profile)}`,
      {
        method: "POST",
        headers: { "X-API-Key": apiKey },
        cache: "no-store",
      },
    );
  } catch {
    return NextResponse.json(
      { error: "Could not reach the ranking service." },
      { status: 502 },
    );
  }

  // Relay status + body verbatim (incl. the 429 cooldown payload + Retry-After).
  const body = await upstream.text();
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const retryAfter = upstream.headers.get("Retry-After");
  if (retryAfter) headers["Retry-After"] = retryAfter;
  return new NextResponse(body || "{}", { status: upstream.status, headers });
}
