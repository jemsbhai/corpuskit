import type { Metadata } from "next";
import { connection } from "next/server";
import type { ReactNode } from "react";

import { AppProviders } from "@/components/app-providers";
import { SiteFooter } from "@/components/site-footer";
import { SiteHeader } from "@/components/site-header";

import "./globals.css";
import "./evaluation.css";
import "./analysis.css";
import "./projects.css";
import "./workbenches.css";

export const metadata: Metadata = {
  title: {
    default: "CorpusKit — corpus design workbench",
    template: "%s — CorpusKit",
  },
  description:
    "A transparent workbench for evaluating, optimizing, and generating speech corpora with CorpusGen.",
};

export default async function RootLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  await connection();
  return (
    <html lang="en" data-scroll-behavior="smooth">
      <body>
        <a className="skip-link" href="#main-content">
          Skip to main content
        </a>
        <AppProviders>
          <div className="site-shell">
            <SiteHeader />
            <main id="main-content">{children}</main>
            <SiteFooter />
          </div>
        </AppProviders>
      </body>
    </html>
  );
}
