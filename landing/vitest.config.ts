import { defineConfig } from "vitest/config";
import path from "path";

export default defineConfig({
  test: {
    // Node, not jsdom: everything under test here is server-side logic — the
    // OIDC state cookie, the origin gate, the tenant predicate. None of it
    // touches a DOM, and pulling one in would be a dependency for nothing.
    environment: "node",
    // {ts,tsx} — NOT "*.test.ts". The narrow pattern does not match .test.tsx,
    // and a test file that is never collected looks EXACTLY like one that
    // passes: green run, rc 0, nothing said. That is the same silence this
    // whole test setup was added to fix, so it must not live inside it.
    // `vitest.config.test.ts` fails if this stops covering tsx.
    include: ["src/**/*.test.{ts,tsx}"],
  },
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
});
