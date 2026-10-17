import type { Metadata } from "next";
import { Instrument_Sans, Newsreader } from "next/font/google";
import type { ReactNode } from "react";
import { PageShell } from "@/components/page-shell";
import "./globals.css";

const reading = Newsreader({ subsets: ["latin"], variable: "--font-reading", display: "swap" });
const interface_ = Instrument_Sans({ subsets: ["latin"], variable: "--font-interface", display: "swap" });

export const metadata: Metadata = {
  title: "InsightForge",
  description: "Submit a research Brief and read a cited intelligence Report.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${reading.variable} ${interface_.variable}`}>
      <body>
        <PageShell>{children}</PageShell>
      </body>
    </html>
  );
}
