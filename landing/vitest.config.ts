import { defineConfig } from "vitest/config";
import path from "path";

export default defineConfig({
  test: {
    // Node, not jsdom: everything under test here is server-side logic — the
    // OIDC state cookie, the origin gate, the tenant predicate. None of it
    // touches a DOM, and pulling one in would be a dependency for nothing.
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
});
