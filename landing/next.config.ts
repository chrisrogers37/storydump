import type { NextConfig } from "next";

const nextConfig: NextConfig = {
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
      // Telegram Login Widget needs 'unsafe-inline' for its injected script
      // and 'unsafe-eval' because telegram-widget.js eval()s the data-onauth handler
      {
        source: "/login",
        headers: [
          {
            key: "Content-Security-Policy",
            value:
              "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://telegram.org; frame-src https://oauth.telegram.org;",
          },
        ],
      },
    ];
  },
};

export default nextConfig;
