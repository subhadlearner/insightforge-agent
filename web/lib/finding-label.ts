import type { Finding } from "@/lib/api";

type FindingDisplay = Pick<Finding, "subject" | "predicate" | "object" | "scope" | "polarity">;

export function findingLabel(f: FindingDisplay): string {
  const polarity = f.polarity === "negated" ? "Negated" : "Affirmed";
  return `${polarity}: ${f.subject} · ${f.predicate} · ${f.object}${f.scope ? ` · ${f.scope}` : ""}`;
}
