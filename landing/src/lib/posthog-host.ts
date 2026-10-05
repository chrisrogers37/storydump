/**
 * PostHog's US ingestion host: where `posthog.ts` sends events, and the one
 * third-party origin the CSP lets the browser connect to (`csp.ts`). Its own
 * module because `next.config.ts` and the middleware load `csp.ts`, and
 * `posthog.ts` reads a browser variable through the `@/` alias.
 */
export const POSTHOG_HOST = "https://us.i.posthog.com"
