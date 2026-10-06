/**
 * Invitations, as the Members card shows them (#1563, #1564).
 *
 * An invitation's join link exists in full only in the answer to the command
 * that made it: the port stores a hash of the token. So the link is shown once,
 * from that answer, and the pending list below never carries one.
 */

export type PendingInvitation = {
  id: string;
  /** Null for a Telegram invitation, which names no address. */
  email: string | null;
  role: string;
  expiresAt: string;
};

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function nonEmptyString(v: unknown): v is string {
  return typeof v === "string" && v !== "";
}

/**
 * The link to show once: the API's own `join_url`, so every client shows the
 * same link. It is null on a deployment with no WEB_APP_URL, and then there is
 * no link to show. The token beside it is never used to build one, because
 * this page's own origin may not be where the invitation lives.
 */
export function joinLinkFrom(data: Record<string, unknown>): string | null {
  return nonEmptyString(data.join_url) ? data.join_url : null;
}

/**
 * The listing's rows. A read that did not return a list is `null`, which the
 * card says it could not load, never `[]`, which would claim there are none.
 * A row missing a field the card shows is dropped rather than filled in.
 */
export function pendingInvitationsFrom(raw: unknown): PendingInvitation[] | null {
  if (!Array.isArray(raw)) return null;
  return raw.flatMap((row): PendingInvitation[] => {
    if (
      !isPlainObject(row) ||
      typeof row.id !== "string" ||
      typeof row.role !== "string" ||
      typeof row.expires_at !== "string"
    ) {
      return [];
    }
    return [
      {
        id: row.id,
        email: typeof row.email === "string" ? row.email : null,
        role: row.role,
        expiresAt: row.expires_at,
      },
    ];
  });
}

const EXPIRY_FORMAT: Intl.DateTimeFormatOptions = { month: "short", day: "numeric" };
const expiryFormatters = new Map<string, { format: Intl.DateTimeFormat; fellBack: boolean }>();

function expiryFormatter(tz: string) {
  let entry = expiryFormatters.get(tz);
  if (!entry) {
    try {
      entry = { format: new Intl.DateTimeFormat("en-US", { ...EXPIRY_FORMAT, timeZone: tz }), fellBack: false };
    } catch {
      entry = { format: new Intl.DateTimeFormat("en-US", { ...EXPIRY_FORMAT, timeZone: "UTC" }), fellBack: true };
    }
    expiryFormatters.set(tz, entry);
  }
  return entry;
}

/**
 * The day an invitation expires, on the WORKSPACE's clock, as `formatSlot`
 * reads a slot: Postgres's long fraction trimmed to the three digits `Date`
 * promises, and an unknown zone rendered in UTC and labelled so.
 */
export function expiryLabel(iso: string, tz: string): string {
  const date = new Date(iso.replace(/\.(\d{3})\d+/, ".$1"));
  const { format, fellBack } = expiryFormatter(tz);
  const label = format.format(date);
  return fellBack ? `${label} UTC` : label;
}
