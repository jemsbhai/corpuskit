"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { LogoMark } from "@/components/logo-mark";
import { ProjectPicker } from "@/components/project-picker";

const navigation = [
  { href: "/", label: "Home" },
  { href: "/g2p", label: "G2P" },
  { href: "/inventory", label: "Inventory" },
  { href: "/coverage", label: "Coverage" },
  { href: "/analysis", label: "Analysis" },
  { href: "/selection", label: "Selection" },
  { href: "/generation", label: "Generation" },
  { href: "/advanced", label: "Advanced" },
  { href: "/jobs", label: "Jobs" },
  { href: "/artifacts", label: "Artifacts" },
  { href: "/evaluate", label: "Evaluate" },
  { href: "/projects", label: "Projects" },
  { href: "/capabilities", label: "Status" },
] as const;

export function SiteHeader() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);

  return (
    <header className="site-header">
      <div className="header-inner section-frame">
        <Link
          aria-label="CorpusKit home"
          className="brand"
          href="/"
          prefetch={false}
        >
          <LogoMark />
          <span>CorpusKit</span>
          <small>alpha</small>
        </Link>

        <button
          aria-controls="primary-navigation"
          aria-expanded={open}
          className="menu-button"
          onClick={() => setOpen((value) => !value)}
          type="button"
        >
          <span className="sr-only">Toggle navigation</span>
          <i aria-hidden="true" />
          <i aria-hidden="true" />
        </button>

        <nav
          aria-label="Primary navigation"
          className={open ? "primary-nav is-open" : "primary-nav"}
          id="primary-navigation"
        >
          {navigation.map((item) => {
            const active = pathname === item.href;
            return (
              <Link
                aria-current={active ? "page" : undefined}
                className={active ? "nav-link is-active" : "nav-link"}
                href={item.href}
                key={item.href}
                onClick={() => setOpen(false)}
                prefetch={false}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
      </div>
      <ProjectPicker />
    </header>
  );
}
