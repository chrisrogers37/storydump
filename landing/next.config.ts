import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // `next dev` writes AGENTS.md and CLAUDE.md into this directory when it
  // detects an AI coding agent. Agents run here and load CLAUDE.md as
  // instructions, so a framework-written one would be instructions this
  // repository never wrote.
  agentRules: false,
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
        ],
      },
    ];
  },
};

export default nextConfig;
