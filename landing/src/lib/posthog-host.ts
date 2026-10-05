/**
 * PostHog's US ingestion host: where `posthog.ts` sends events, and the one
 * third-party origin the CSP lets the browser connect to (`csp.ts`). Its own
 * module because `next.config.ts` loads `csp.ts`, and the library must not
 * come with it.
 */
export const POSTHOG_HOST = "https://us.i.posthog.com"
