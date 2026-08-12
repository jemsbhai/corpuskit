import type { NextConfig } from "next";
import path from "node:path";

const securityHeaders = [
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  {
    key: "Permissions-Policy",
    value: "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
  },
  { key: "Referrer-Policy", value: "no-referrer" },
  {
    key: "Strict-Transport-Security",
    value: "max-age=31536000; includeSubDomains",
  },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
] as const;

const nonHtmlContentSecurityPolicy =
  "default-src 'none'; base-uri 'none'; frame-ancestors 'none'";

const nextConfig: NextConfig = {
  output: "standalone",
  outputFileTracingRoot: path.join(process.cwd(), "../.."),
  poweredByHeader: false,
  reactStrictMode: true,
  typedRoutes: true,
  async headers() {
    return [
      {
        source: "/api/:path*",
        headers: [
          ...securityHeaders,
          {
            key: "Content-Security-Policy",
            value: nonHtmlContentSecurityPolicy,
          },
        ],
      },
      {
        source: "/auth/:path*",
        headers: [
          ...securityHeaders,
          {
            key: "Content-Security-Policy",
            value: nonHtmlContentSecurityPolicy,
          },
        ],
      },
      { source: "/:path*", headers: [...securityHeaders] },
    ];
  },
};

export default nextConfig;
