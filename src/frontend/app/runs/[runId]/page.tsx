import { RunView } from "@/components/run-view";

export default async function RunPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  const id = decodeURIComponent(runId);
  return <RunView key={id} runId={id} />; // fresh progress state and stream per Run
}
