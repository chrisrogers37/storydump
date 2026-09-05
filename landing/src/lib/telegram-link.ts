import { isHttpsUrlOnHost } from "./redirect-guard";
import { notAuthenticatedCopy, unreachableCopy } from "./refusal-copy";
import { botName } from "./telegram-bot";

/**
 * Linking a Telegram identity, browser side (#1172 clause-1 wiring; #1157).
 *
 * The API mints a one-shot `t.me/<bot>?start=link-<state>` link for the
 * signed-in user (`POST /api/v1/me/telegram/link`); tapping it in Telegram
 * sends `/start link-<state>` to the bot, whose webhook attaches the tapping
 * Telegram account to that user. This module asks for the link, guards it
 * before it is opened, and reads "already linked" off the session's
 * identities.
 */

/** The one host a link from this flow may point at. */
const TELEGRAM_HOST = "t.me";
const LINK_PREFIX = "link-";

/**
 * HTTPS, Telegram's short host exactly (equality on `host`, so a lookalike or
 * a non-default port is refused), the PRODUCT'S bot when the site knows it
 * (`NEXT_PUBLIC_TELEGRAM_BOT_NAME` — a link to some other bot must not be
 * opened from a control labelled "link your account"), and a `start` payload
 * of THIS flow's kind — an invitation link (`inv-`) is a different door.
 */
export function isTelegramLink(value: string, bot: string | undefined = botName): boolean {
  if (!isHttpsUrlOnHost(value, TELEGRAM_HOST)) return false;
  const parsed = new URL(value);
  if (bot && parsed.pathname !== `/${bot}`) return false;
  const start = parsed.searchParams.get("start") ?? "";
  return start.startsWith(LINK_PREFIX) && start.length > LINK_PREFIX.length;
}

export type TelegramLinkResult =
  | { ok: true; link: string; expiresInSeconds: number }
  | { ok: false; error: string; status: number };

/** Ask for a fresh link. Returns it rather than opening it: the caller owns
 *  the navigation, so a refusal renders instead of a page that did nothing. */
export async function requestTelegramLink(): Promise<TelegramLinkResult> {
  let response: Response;
  try {
    response = await fetch("/api/me/telegram/link", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return { ok: false, error: "unreachable", status: 0 };
  }

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = typeof data?.error === "string" ? data.error : `http_${response.status}`;
    return { ok: false, error, status: response.status };
  }

  const link = data?.link;
  if (typeof link !== "string" || !isTelegramLink(link)) {
    return { ok: false, error: "malformed_link", status: response.status };
  }
  const expiresInSeconds =
    typeof data?.expiresInSeconds === "number" ? data.expiresInSeconds : 0;
  return { ok: true, link, expiresInSeconds };
}

/** A sentence for a refusal. Every branch says what happened to the data. */
export function telegramLinkRefusalCopy(reason: unknown): string {
  switch (reason) {
    case "http_503":
      return "Telegram linking is not set up on this deployment yet. Nothing changed.";
    case "unauthenticated":
    case "http_401":
      return notAuthenticatedCopy("Nothing changed.");
    case "malformed_link":
      return "Storydump could not produce a Telegram link. Nothing changed — report this if it repeats.";
    case "unreachable":
    case "target_router_unreachable":
      return unreachableCopy("Nothing changed");
  }
  return "Could not start Telegram linking. Nothing changed — try again shortly.";
}

/** Whether the user already has a Telegram identity attached, from `/me`'s
 *  identities. An unreadable list is "not linked", never "linked". */
export function telegramLinkedFrom(
  identities: ReadonlyArray<{ provider: string }> | null | undefined,
): boolean {
  return telegramIdentityFrom(identities) !== null;
}

/**
 * The attached Telegram identity's display name, or null. Shown beside
 * "Linked" so a person can tell WHOSE Telegram is on their account — the one
 * check that makes a link tapped by the wrong person visible.
 */
export function telegramIdentityFrom(
  identities:
    | ReadonlyArray<{ provider: string; display_name?: string | null }>
    | null
    | undefined,
): { displayName: string | null } | null {
  if (!Array.isArray(identities)) return null;
  const found = identities.find((i) => i.provider === "telegram");
  if (!found) return null;
  const name = typeof found.display_name === "string" ? found.display_name.trim() : "";
  return { displayName: name || null };
}

// --- Telegram groups: the workspace-level bind link (`07` §13) ---------------

const BIND_PREFIX = "bind-";

/**
 * Our bot's `startgroup` link with a `bind-` payload — the link that opens
 * Telegram's group picker and binds the chosen group to a workspace. Pinned to
 * the configured bot for the same reason `isTelegramLink` is.
 */
export function isTelegramGroupLink(value: string, bot: string | undefined = botName): boolean {
  if (!isHttpsUrlOnHost(value, TELEGRAM_HOST)) return false;
  const parsed = new URL(value);
  if (bot && parsed.pathname !== `/${bot}`) return false;
  const start = parsed.searchParams.get("startgroup") ?? "";
  return start.startsWith(BIND_PREFIX) && start.length > BIND_PREFIX.length;
}

export async function requestTelegramGroupLink(workspaceId: string): Promise<TelegramLinkResult> {
  let response: Response;
  try {
    response = await fetch(`/api/workspaces/${workspaceId}/telegram/bind-link`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return { ok: false, error: "unreachable", status: 0 };
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = typeof data?.error === "string" ? data.error : `http_${response.status}`;
    return { ok: false, error, status: response.status };
  }
  const link = data?.link;
  if (typeof link !== "string" || !isTelegramGroupLink(link)) {
    return { ok: false, error: "malformed_link", status: response.status };
  }
  const expiresInSeconds = typeof data?.expiresInSeconds === "number" ? data.expiresInSeconds : 0;
  return { ok: true, link, expiresInSeconds };
}

export function telegramGroupLinkRefusalCopy(reason: unknown): string {
  switch (reason) {
    case "insufficient_role":
    case "http_403":
      return "You need to be an admin of this workspace to add a Telegram group.";
    case "http_503":
      return "Telegram is not set up on this deployment yet. Nothing changed.";
  }
  return telegramLinkRefusalCopy(reason);
}
