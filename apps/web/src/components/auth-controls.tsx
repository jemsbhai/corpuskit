"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  authenticatedFetch,
  browserSession,
  clearBrowserSessionCache,
  type BrowserSessionView,
} from "@/lib/browser-auth";
import { clearSelectedProject } from "@/components/project-context";

export function AuthControls() {
  const router = useRouter();
  const [session, setSession] = useState<BrowserSessionView | null>();
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    void browserSession().then(
      (value) => {
        if (active) setSession(value);
      },
      () => {
        if (active) setSession(null);
      },
    );
    return () => {
      active = false;
    };
  }, []);

  if (session === undefined) {
    return <span className="auth-status">Checking session…</span>;
  }
  if (session === null) {
    return (
      <Link className="auth-link" href="/auth/login?returnTo=%2F">
        Sign in securely
      </Link>
    );
  }
  return (
    <div className="auth-controls">
      <span className="auth-status">
        Signed in as {session.displayName ?? session.subject}
      </span>
      <button
        disabled={busy}
        onClick={() => {
          setBusy(true);
          void authenticatedFetch("/auth/logout", { method: "POST" }).then(
            (response) => {
              clearBrowserSessionCache();
              if (response.ok) {
                clearSelectedProject();
                router.push("/");
                router.refresh();
              } else setBusy(false);
            },
            () => setBusy(false),
          );
        }}
        type="button"
      >
        {busy ? "Signing out…" : "Sign out"}
      </button>
    </div>
  );
}
