"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { PersonaPicker } from "@/components/persona-picker";
import { PersonaProvider } from "@/components/persona-context";

export function PageShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  if (pathname.startsWith("/share/")) {
    return (
      <>
        <a className="skip-link" href="#main">Skip to content</a>
        <header className="masthead">
          <span className="wordmark">InsightForge</span>
        </header>
        <main id="main" className="page">{children}</main>
      </>
    );
  }
  return (
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
  );
}
