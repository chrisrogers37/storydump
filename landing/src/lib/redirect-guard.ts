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
