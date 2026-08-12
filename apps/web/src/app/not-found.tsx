import Link from "next/link";

export default function NotFound() {
  return (
    <section
      className="section-frame empty-page"
      aria-labelledby="not-found-title"
    >
      <p className="eyebrow">404 · Outside the inventory</p>
      <h1 id="not-found-title">That page is not part of this corpus.</h1>
      <p>The route may be planned but is not available in this scaffold.</p>
      <Link className="button button-primary" href="/">
        Return to the dashboard
      </Link>
    </section>
  );
}
