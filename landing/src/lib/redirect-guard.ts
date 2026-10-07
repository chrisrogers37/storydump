/**
 * The one guard every "hand the browser to a provider" flow runs on the line
 * before it navigates: HTTPS, and the expected host EXACTLY.
 *
 * Equality on `host`, never `endsWith` — `evil-accounts.google.com` and
 * `accounts.google.com.evil.example` both satisfy a suffix check, and that is
 * the real trap here. `host` rather than `hostname` because it carries a
 * non-default port, so `https://accounts.google.com:1234` is refused while the
 * ordinary URL (default port omitted from `host`) passes.
 *
 * Each flow wraps this with its own host constant; the check lives once so a
 * fix to it (IDN handling, say) reaches every flow.
 */
export function isHttpsUrlOnHost(value: string, host: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  return parsed.protocol === "https:" && parsed.host === host;
}

/**
 * The `href` for a link a person typed, or null when it may not become one.
 * An item's link to add by hand reaches the page as an anchor only when it is
 * https, names a host, and carries no user name or password; anything else is
 * shown as text. The port refuses such links on the way in; this guards the way
 * out as well, for a value stored before that rule or written by another door.
 *
 * Parsed, never prefix-matched: `new URL` reads the scheme the way the browser
 * will, so case, stray whitespace and look-alike prefixes cannot slip through.
 */
export function httpsHref(value: string | null | undefined): string | null {
  if (!value) return null;
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }
  if (parsed.protocol !== "https:" || !parsed.hostname) return null;
  if (parsed.username || parsed.password) return null;
  return parsed.href;
}
