import Link from "next/link";

import { AuthControls } from "@/components/auth-controls";

export function SiteFooter() {
  return (
    <footer className="site-footer">
      <div className="footer-inner section-frame">
        <div>
          <strong>CorpusKit</strong>
          <p>
            Alpha corpus-design application powered by CorpusGen. Connected
            deployments keep OIDC tokens and provider credentials server-side.
          </p>
        </div>
        <nav aria-label="Footer navigation">
          <Link href="/capabilities" prefetch={false}>
            Capability status
          </Link>
          <AuthControls />
          <a href="https://github.com/jemsbhai/corpuskit">CorpusKit source</a>
          <a href="https://github.com/jemsbhai/corpusgen">CorpusGen source</a>
        </nav>
        <p className="footer-status">
          <i aria-hidden="true" /> Alpha · capability-gated
        </p>
      </div>
    </footer>
  );
}
