import type { NextConfig } from "next";

// The browser only ever talks to this Next origin. `/api/*` is rewritten to FastAPI, so the
// signed HttpOnly persona cookie and the SSE EventSource stay same-origin and no CORS is
// needed (design.md section 13). INSIGHTFORGE_API_ORIGIN is read by the Next server only;
// `next dev` reads it at start, `next build` bakes it into the build that `next start` serves.
const apiOrigin = (process.env.INSIGHTFORGE_API_ORIGIN ?? "http://127.0.0.1:8000").replace(/\/$/, "");

const nextConfig: NextConfig = {
  agentRules: false, // do not generate src/frontend/AGENTS.md on `next dev`
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiOrigin}/:path*` }];
  },
};

export default nextConfig;
