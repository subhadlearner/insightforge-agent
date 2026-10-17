import type { Metadata } from "next";
import { Instrument_Sans, Newsreader } from "next/font/google";
import Link from "next/link";
import type { ReactNode } from "react";
import { PersonaPicker } from "@/components/persona-picker";
import { PersonaProvider } from "@/components/persona-context";
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
        <PersonaProvider>
          <a className="skip-link" href="#main">Skip to content</a>
          <header className="masthead">
            <Link href="/" className="wordmark">InsightForge</Link>
            <nav className="masthead__nav" aria-label="Research navigation">
              <Link href="/watchlist">Watchlist</Link>
              <Link href="/alerts">Alerts</Link>
            </nav>
            <PersonaPicker />
          </header>
          <main id="main" className="page">{children}</main>
        </PersonaProvider>
      </body>
    </html>
  );
}
