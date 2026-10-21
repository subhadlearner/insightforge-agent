// Regenerates lib/api-schema.d.ts from a running FastAPI's OpenAPI schema.
// INSIGHTFORGE_API_ORIGIN selects the backend (default http://127.0.0.1:8000).
import { execFileSync } from "node:child_process";

const origin = process.env.INSIGHTFORGE_API_ORIGIN ?? "http://127.0.0.1:8000";
execFileSync(
  process.execPath,
  ["node_modules/openapi-typescript/bin/cli.js", `${origin}/openapi.json`, "-o", "lib/api-schema.d.ts"],
  { stdio: "inherit" },
);
