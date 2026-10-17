import { BriefForm } from "@/components/brief-form";
import { RecentReports } from "@/components/recent-reports";

export default function HomePage() {
  return (
    <>
      <h1 className="page__title">What should we find out?</h1>
      <BriefForm />
      <RecentReports />
    </>
  );
}
