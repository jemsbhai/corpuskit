"use client";

import type { ReactNode } from "react";

import { ProjectProvider } from "@/components/project-context";

export function AppProviders({ children }: { readonly children: ReactNode }) {
  return <ProjectProvider>{children}</ProjectProvider>;
}
