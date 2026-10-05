/**
 * PostHog's US ingestion host: where `posthog.ts` sends events, and the one
 * third-party origin the CSP lets the browser connect to (`csp.ts`). It is a
 * module of its own so that `csp.ts`, which `next.config.ts` and the
 * middleware load, imports nothing but this constant.
 */
export const POSTHOG_HOST = "https://us.i.posthog.com"
