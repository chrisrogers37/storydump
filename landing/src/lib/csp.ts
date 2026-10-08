/**
 * The Content-Security-Policy a page is served with. There are two, chosen by
 * how the page is rendered, and they differ only in `script-src`.
 *
 * A page RENDERED PER REQUEST gets `noncePolicy`, from middleware. These are
 * the pages that read the session, an invite or the query string, so they are
 * where untrusted strings render: workspace, folder and display names. Each
 * request gets a fresh nonce, Next stamps it on every script it renders, and
 * an inline script without it does not run.
 *
 * A page PRERENDERED at build time gets `staticPagePolicy`, from
 * `next.config.ts`. Its HTML exists before any request does, so it cannot
 * carry a nonce. It cannot be hashed either: each page's inline RSC payload
 * names build-specific chunk files, and the header is fixed before the pages
 * are generated. So its inline scripts run under 'unsafe-inline'. These pages
 * are built from this repository alone and render nothing a visitor supplied.
 * Rendering them per request to give them a nonce would take them off the CDN.
 */

import { POSTHOG_HOST } from "./posthog-host";

/** Every directive but `script-src`, the same in both policies. */
const SHARED = [
  "default-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  // Every picture is this tier's own, thumbnails included: they stream through
  // its thumbnail route, so a page never loads an image from another host.
  "img-src 'self' data:",
  "font-src 'self'",
  // The browser calls only this tier and PostHog's ingestion host. Signing in
  // is a navigation, not a fetch.
  `connect-src 'self' ${POSTHOG_HOST}`,
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
];

type Options = { dev?: boolean };

function policy(scriptSources: string[], { dev = false }: Options): string {
  // In development React evaluates code to rebuild server-side error stacks.
  const sources = dev ? [...scriptSources, "'unsafe-eval'"] : scriptSources;
  return [`script-src ${sources.join(" ")}`, ...SHARED].join("; ");
}

/** For a prerendered page. */
export function staticPagePolicy(options: Options = {}): string {
  return policy(["'self'", "'unsafe-inline'"], options);
}

/**
 * For a page rendered per request. 'strict-dynamic' lets a nonced script load
 * others (Next's chunks, the analytics library among them); 'self' is for
 * browsers that predate it.
 */
export function noncePolicy(nonce: string, options: Options = {}): string {
  return policy(["'self'", `'nonce-${nonce}'`, "'strict-dynamic'"], options);
}

/** 128 random bits, base64. */
export function createNonce(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return btoa(String.fromCharCode(...bytes));
}

/**
 * Paths whose pages send the nonce policy as Report-Only: a violation is
 * logged in the browser console and nothing is blocked. A page moves to
 * enforcement once it has been loaded, in every state it renders, with no
 * violation logged.
 */
const REPORT_ONLY = ["/dashboard", "/welcome", "/workspaces"];

export function isReportOnly(pathname: string): boolean {
  return REPORT_ONLY.some((path) => pathname === path || pathname.startsWith(`${path}/`));
}
