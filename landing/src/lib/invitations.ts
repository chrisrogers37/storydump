/**
 * Invitations, as the Members card shows them, and as the person
 * invited sees their own (invitee invitation functions).
 *
 * An invitation's join link exists in full only in the answer to the command
 * that made it, and in its email: the port stores a hash of the token. So the
 * link is shown once, from that answer, and no list here ever carries one.
 */

import { callBff, postJson } from "./bff";
import { notAuthenticatedCopy } from "./refusal-copy";

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
 * What became of the invitation's email, and whose day refused it when it was
 * over budget: `vocabulary.INVITATION_DELIVERY_STATES` and
 * `EMAIL_BUDGET_REASONS`, pinned by `wire-contract.test.ts`.
 */
export const DELIVERY_STATES = ["sent", "not_configured", "over_budget", "failed"] as const;
export type DeliveryState = (typeof DELIVERY_STATES)[number];
export const OVER_BUDGET_REASONS = ["person_limit", "daily_limit"] as const;
export type Delivery = { state: DeliveryState; reason: string | null };

/**
 * The answer's `delivery`. A state this page does not know, or none, reads as
 * `failed`: nothing here may claim an email went out that it cannot confirm.
 */
export function deliveryFrom(data: Record<string, unknown>): Delivery {
  const raw = isPlainObject(data.delivery) ? data.delivery : {};
  const state = DELIVERY_STATES.find((s) => s === raw.state) ?? "failed";
  return { state, reason: nonEmptyString(raw.reason) ? raw.reason : null };
}

const SEND_IT = "Send this link to them — it works once.";

/**
 * The first line of the shown-once block: whether the invitation was emailed,
 * and so how much the link on screen matters.
 */
export function inviteLinkHeadline(delivery: Delivery, email: string): string {
  if (delivery.state === "sent") {
    return `Invitation emailed to ${email}. If it doesn't arrive, send them this link — it works once.`;
  }
  const why =
    delivery.state === "not_configured"
      ? "Email isn't set up on this deployment, so nothing was sent."
      : delivery.state === "over_budget"
        ? delivery.reason === "person_limit"
          ? "You've sent today's limit of invitation emails, so this one wasn't emailed."
          : "Storydump has reached today's email limit, so this one wasn't emailed."
        : `We couldn't email ${email} just now.`;
  return `${why} ${SEND_IT}`;
}

/** An invitation just made, as the inviter is shown it once. */
export type MintedInvite = { id: string; link: string; email: string; delivery: Delivery };

/**
 * The answer to `invite_member` → the invitation to show once, or null when
 * there is no link to show. Its id is what a later Revoke of that same
 * invitation matches, so the link comes down with the invitation it opens.
 */
export function mintedInviteFrom(
  data: Record<string, unknown>,
  email: string,
): MintedInvite | null {
  const link = joinLinkFrom(data);
  const id = data.invitation_id;
  return link !== null && nonEmptyString(id)
    ? { id, link, email, delivery: deliveryFrom(data) }
    : null;
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

/** An invitation addressed to the signed-in person, as `/me/invitations` lists it (invitee invitation functions). */
export type InvitationForYou = {
  id: string;
  workspaceName: string;
  role: string;
  expiresAt: string;
  /** The inviter's Google name, or null. */
  inviterName: string | null;
};

/**
 * What `GET /api/v1/me/invitations` answers: the invitations, and whether
 * "Email me the link again" can send on this deployment. Without email set
 * up it could only answer `not_configured`, so the panel asks the inviter for
 * the link instead of offering a button.
 */
export type MyInvitations = { invitations: InvitationForYou[]; canResend: boolean };

/**
 * The answer → its invitations, or null when it holds no list: the panel then
 * says it couldn't check, never that there are none. A row missing a field the
 * panel shows is dropped. Re-sending is offered only when the API says so.
 */
export function invitationsForYouFrom(data: unknown): MyInvitations | null {
  const raw = isPlainObject(data) ? data.invitations : undefined;
  if (!Array.isArray(raw)) return null;
  const invitations = raw.flatMap((row): InvitationForYou[] => {
    if (
      !isPlainObject(row) ||
      !nonEmptyString(row.invitation_id) ||
      typeof row.workspace_name !== "string" ||
      typeof row.role !== "string" ||
      !nonEmptyString(row.expires_at)
    ) {
      return [];
    }
    return [
      {
        id: row.invitation_id,
        workspaceName: row.workspace_name,
        role: row.role,
        expiresAt: row.expires_at,
        inviterName: nonEmptyString(row.inviter_name) ? row.inviter_name : null,
      },
    ];
  });
  return { invitations, canResend: isPlainObject(data) && data.resend === true };
}

/** Who invited them, and to what: "Dana invited you to Acme as an admin." */
export function invitedLine(invitation: InvitationForYou): string {
  const workspace = invitation.workspaceName.trim() || "a workspace";
  const as = invitation.role === "admin" ? " as an admin" : "";
  return invitation.inviterName
    ? `${invitation.inviterName} invited you to ${workspace}${as}.`
    : `You're invited to ${workspace}${as}.`;
}

/**
 * Where the link is. The token is the only way in, so the panel never offers
 * to join. It doesn't say the link came by email: without email set up, or
 * when an invitation's email didn't go, the inviter shared it by hand. The
 * date is UTC, as the email gives it.
 */
export function whereTheLinkIs(invitation: InvitationForYou): string {
  return `Open the invitation link you were sent to join. It expires ${expiryLabel(invitation.expiresAt, "UTC")} (UTC).`;
}

/** Where re-sending can't work here: the person to ask instead. */
export function askForTheLink(invitation: InvitationForYou): string {
  return `Don't have the link? Ask ${invitation.inviterName ?? "the person who invited you"} to send it to you.`;
}

/** The answer to "Email me the link again": what became of the email, or why it was refused. */
export type LinkSentAgain =
  | { ok: true; delivery: Delivery }
  | { ok: false; error: string; status: number };

/** "Email me the link again" (invitee invitation functions), through this tier's route. */
export async function sendLinkAgain(invitationId: string): Promise<LinkSentAgain> {
  const result = await callBff(
    `/api/me/invitations/${encodeURIComponent(invitationId)}/resend`,
    postJson({}),
  );
  return result.ok
    ? { ok: true, delivery: deliveryFrom(result.data) }
    : { ok: false, error: result.error, status: result.status };
}

const ASK_THEM = "ask the person who invited you to send you the link";

/**
 * A sentence for a refused "Email me the link again". A refusal replaced
 * nothing. A lost answer (no answer, or a server's error) is not a refusal:
 * the email may have gone and the link been replaced, so it says to look
 * before asking again.
 */
export function linkSentAgainRefusalCopy(reason: unknown, status?: number): string {
  switch (reason) {
    case "not_acceptable":
      return "This invitation is no longer open. Ask the person who invited you for a new one.";
    case "unauthenticated":
    case "http_401":
      return notAuthenticatedCopy("Nothing changed.");
  }
  if (!status || status >= 500) {
    return "We couldn't confirm whether the email went. Check your inbox before asking again.";
  }
  return "Could not ask for the link. Nothing changed — try again shortly.";
}

/**
 * One line for the answer. The link is replaced only once the email has gone,
 * so `sent` is the one answer after which the old link no longer works: a
 * spent day, an unconfigured deployment and a failed send all leave it working.
 */
export function linkSentAgainCopy(answer: LinkSentAgain, email: string | null): string {
  if (!answer.ok) return linkSentAgainRefusalCopy(answer.error, answer.status);
  const { state, reason } = answer.delivery;
  if (state === "sent") {
    return `Sent. A new link is on its way to ${email ?? "your inbox"}, and the old one no longer works.`;
  }
  if (state === "not_configured") return `Storydump can't send email yet, so ${ASK_THEM}.`;
  if (state === "over_budget") {
    return reason === "person_limit"
      ? "You've asked for the link as many times as you can today. The newest link you were sent still works, or try again tomorrow."
      : `Storydump has sent all the email it can today. Your link still works; try again tomorrow, or ${ASK_THEM}.`;
  }
  return "We couldn't send the email just now, so the link you have still works. Try again in a moment.";
}
