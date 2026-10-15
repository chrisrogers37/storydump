/**
 * The API's base URL: where the server-side client calls (`target-api.ts`,
 * which re-exports it), where sign-in goes, and the origin the CSP lets a form
 * send the browser to (`csp.ts`). `TARGET_API_URL` wins, then `BACKEND_URL`,
 * then a local API. It is a module of its own, as `posthog-host.ts` is, so that
 * `csp.ts`, which `next.config.ts` and the middleware load, imports nothing but
 * constants.
 */
export const TARGET_API_URL =
  process.env.TARGET_API_URL || process.env.BACKEND_URL || "http://localhost:8000";
