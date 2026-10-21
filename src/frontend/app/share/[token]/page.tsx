import { SharedReportView } from "@/components/shared-report-view";

export default async function SharedReportPage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  return <SharedReportView key={token} token={decodeURIComponent(token)} />;
}
