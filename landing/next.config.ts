import type { NextConfig } from "next";
import { staticPagePolicy } from "./src/lib/csp";

const nextConfig: NextConfig = {
  // `next dev` writes AGENTS.md and CLAUDE.md into this directory when it
  // detects an AI coding agent. Agents run here and load CLAUDE.md as
  // instructions, so a framework-written one would be instructions this
  // repository never wrote.
  agentRules: false,
  experimental: {
    // The stylesheet goes inside the HTML instead of a second request the
    // browser must wait for before it paints. Most visits are one page from a
    // search or a link, so the headline paints sooner (use-case page LCP 1.85
    // -> 1.0 s, home 2.0 -> 1.2 s, Lighthouse with Slow 4G throttling). The
    // CSP already allows inline styles (`src/lib/csp.ts`). The flag is
    // app-wide, so a full load of a dashboard page carries the ~18 KB
    // (gzipped) stylesheet too; navigations inside the app don't.
    inlineCss: true,
    // Off: on a restored .next/cache, builds intermittently mixed new utilities with stale globals.css rules (#1581).
    turbopackFileSystemCacheForBuild: false,
  },
  async redirects() {
    return [
      // Retired setup pages. Both walked the reader through registering their
      // own Meta app or Google Cloud project; credentials are deployment-level
      // and no surface accepts a tenant-supplied one. Temporary rather than
      // permanent: what replaces this part of the funnel is still open, and a
      // 308 is cached by the browser in a way that is awkward to take back.
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
        source: "/((?!dashboard(?:/|$)|workspaces$|welcome$|join/[^/]+$|auth/error$).*)",
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
