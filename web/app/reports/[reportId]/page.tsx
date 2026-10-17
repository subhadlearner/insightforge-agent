import { ReportView } from "@/components/report-view";

export default async function ReportPage({ params }: { params: Promise<{ reportId: string }> }) {
  const { reportId } = await params;
  return <ReportView reportId={decodeURIComponent(reportId)} />;
}
