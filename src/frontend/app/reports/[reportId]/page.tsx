import { ReportView } from "@/components/report-view";

export default async function ReportPage({ params }: { params: Promise<{ reportId: string }> }) {
  const { reportId } = await params;
  const id = decodeURIComponent(reportId);
  return <ReportView key={id} reportId={id} />;
}
