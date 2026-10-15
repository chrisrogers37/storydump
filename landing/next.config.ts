import type { NextConfig } from "next";
import { staticPagePolicy } from "./src/lib/csp";

const nextConfig: NextConfig = {
  agentRules: false,
  // The stylesheet stays a `<link>`; `experimental.inlineCss` stays off. React
  // writes every `<title>` and `<meta>` after the stylesheets, so an inlined
  // stylesheet (about 96 KB) pushes a page's link-preview tags that far into
  // the HTML, past what a link preview reads. Inlining would paint the
  // headline sooner on a cold load, which is worth it only for a small public
  // stylesheet. `link-preview-head-contract.test.ts` holds this.
  experimental: {
    // Off: on a restored .next/cache, builds can mix new utilities with stale globals.css rules (#1581).
    turbopackFileSystemCacheForBuild: false,
  },
  async redirects() {
    return [
      // Setup paths for registering a tenant's own Meta app or Google Cloud
      // project, which no page offers: credentials are deployment-level and no
      // surface accepts a tenant-supplied one. Temporary rather than permanent:
      // a 308 is cached by the browser in a way that is awkward to take back.
      { source: "/setup/meta-developer", destination: "/setup", permanent: false },
      { source: "/setup/google-drive", destination: "/setup", permanent: false },
    ];
  },
  async headers() {
    return [
      // Global security headers
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
          // This host only. `includeSubDomains` would bind every subdomain of
          // the product domain to HTTPS for the whole max-age, in every
          // browser that has seen it; add it only once every host in the DNS
          // zone is known to serve HTTPS. The max-age is the one the platform
          // already sends for a custom domain, which a shorter value here
          // would replace.
          { key: "Strict-Transport-Security", value: "max-age=63072000" },
        ],
      },
      // The static policy (`src/lib/csp.ts`), for every path but the pages
      // rendered per request, which get the nonce policy from middleware. The
      // exclusions mirror middleware's matcher; `csp-routing-contract.test.ts`
      // holds the two in step.
      {
        source:
          "/((?!dashboard(?:/|$)|workspaces$|welcome$|join/[^/]+$|claim/[^/]+$|auth/error$|ops(?:/|$)).*)",
        headers: [
          {
            key: "Content-Security-Policy",
            value: staticPagePolicy({ dev: process.env.NODE_ENV === "development" }),
          },
        ],
      },
    ];
  },
};

export default nextConfig;
