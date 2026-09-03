import { notAuthenticatedCopy } from "./refusal-copy";

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
 * a non-default port is refused), and a `start` payload of THIS flow's kind —
 * an invitation link (`inv-`) is a different door and must not be opened from
 * a control labelled "link your account".
 */
export function isTelegramLink(value: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  if (parsed.protocol !== "https:" || parsed.host !== TELEGRAM_HOST) return false;
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
      return "Storydump cannot reach the server right now. Nothing changed — try again shortly.";
  }
  return "Could not start Telegram linking. Nothing changed — try again shortly.";
}

/** Whether the user already has a Telegram identity attached, from `/me`'s
 *  identities. An unreadable list is "not linked", never "linked". */
export function telegramLinkedFrom(
  identities: ReadonlyArray<{ provider: string }> | null | undefined,
): boolean {
  return Array.isArray(identities) && identities.some((i) => i.provider === "telegram");
}
