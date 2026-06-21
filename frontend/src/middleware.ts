import { NextRequest, NextResponse } from "next/server";

/**
 * Optional HTTP Basic Auth gate for the whole site, so this can run as a
 * private, advisor-facing demo rather than a public page.
 *
 * Enable by setting BOTH env vars in Vercel (Project → Settings → Environment
 * Variables):
 *   BASIC_AUTH_USER
 *   BASIC_AUTH_PASS
 *
 * If either is unset (e.g. local dev), the gate is a no-op and the site is open.
 * Runs on Vercel's Edge runtime (free, no Pro plan required).
 */
export function middleware(req: NextRequest) {
  const user = process.env.BASIC_AUTH_USER;
  const pass = process.env.BASIC_AUTH_PASS;

  // Not configured → don't gate (keeps local dev / unconfigured deploys open).
  if (!user || !pass) return NextResponse.next();

  const header = req.headers.get("authorization");
  if (header?.startsWith("Basic ")) {
    try {
      const [u, p] = atob(header.slice(6)).split(":");
      if (u === user && p === pass) return NextResponse.next();
    } catch {
      // fall through to 401
    }
  }

  return new NextResponse("Authentication required.", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="Restricted", charset="UTF-8"' },
  });
}

// Gate everything except Next's static assets and the favicon.
export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
