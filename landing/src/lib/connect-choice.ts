import { callBff } from "./bff";
import {
  destinationConnectRefusalCopy,
  requestDestinationConnect,
  requestWorkspaceConnect,
} from "./destination";
import { bareHandle, isDecimalString } from "./source-mix";
import { SOURCE_REFUSAL_COPY, sourceRefusalCopy } from "./source-refusals";

/**
 * The past-posts choice an Instagram connect makes BEFORE it leaves for
 * Instagram (the API's Connect preview and Start resources, #1644).
 *
 * Every connect asks the API first (`connect-preview`). Where past posts are
 * not a choice (`archive_available` false, which is every workspace by
 * default), the start goes ahead at once: no dialog, and the
 * body `{}`. Where they are, the Accounts tab opens a dialog over the preview,
 * and the start carries the choice and the revision the preview was made at.
 * A start the mix has moved past is refused (409 `mix_conflict`) before any
 * OAuth state exists, so the dialog fetches a fresh preview and asks again:
 * the person never leaves on a preview that is no longer true.
 *
 * A preview that cannot be had at all is the first case, not a refusal. This
 * tier and the API deploy independently, so a web that is ahead of its API asks
 * for a route that is not there yet (a 404), and a connect that refused to start
 * on that would break connecting Instagram in production. Whatever the failure
 * (a 404, a 5xx, an answer that is not a preview, a dropped connection), the
 * start goes ahead at once: no dialog, and the body `{}`. That is
 * always valid: where past posts are live, a new account then lands with them
 * Off and the banner after the connect says so. It is only the first preview
 * that falls back; see `confirmConnect` for the refresh after a 409.
 *
 * Nothing here decides what a choice does. The entry, the default, the saved
 * setting a reconnect keeps and the shares an On choice would leave are all
 * the server's; this only reads them, says them, and sends the choice back.
 */

export type ConnectEntry = "add" | "first_connect" | "reconnect";

/** One row of the mix an On choice would leave. */
export type ResetRow = {
  /** Null for the account being connected, whose source does not exist yet. */
  sourceId: string | null;
  provider: string;
  /** A folder's name, an archive's handle; null for an account not yet known. */
  name: string | null;
  /** The share after the reset, as sent; `"0"` is a source that stays Off. */
  targetShare: string;
};

export type ConnectPreview = {
  /** Whether past posts are a choice here at all. */
  archiveAvailable: boolean;
  entry: ConnectEntry;
  /** The named account's handle, without the `@`; null for the header's Add. */
  handle: string | null;
  /** The switch's default (add, first connect). */
  defaultOn: boolean;
  /** The setting a reconnect keeps; null for every other entry. */
  savedEnabled: boolean | null;
  /** The revision a choice is checked against; null where there is no choice. */
  expectedMixRevision: number | null;
  previewVersion: number;
  /** The equal shares an On choice would leave; null when nothing would reset. */
  reset: ResetRow[] | null;
};

/** The one preview shape this page knows how to show and answer. */
const PREVIEW_VERSION = 1;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readResetRow(raw: unknown): ResetRow | null {
  if (!isRecord(raw) || typeof raw.provider !== "string" || !isDecimalString(raw.target_share)) {
    return null;
  }
  if (raw.source_id !== null && typeof raw.source_id !== "string") return null;
  return {
    sourceId: raw.source_id,
    provider: raw.provider,
    name: typeof raw.name === "string" && raw.name.trim() ? raw.name : null,
    targetShare: raw.target_share,
  };
}

/**
 * A preview as the dialog's model, or null when it is not one this page can
 * answer. STRICT where the start is built from it: a choice is sent with the
 * revision and the version read here, so a preview missing either, or of a
 * version this page does not know, starts nothing rather than a guess. The
 * reset rows are what the switch promises, so a malformed one is no preview.
 */
export function parseConnectPreview(data: unknown): ConnectPreview | null {
  if (!isRecord(data) || typeof data.archive_available !== "boolean") return null;
  const { entry, history } = data;
  if (entry !== "add" && entry !== "first_connect" && entry !== "reconnect") return null;
  if (!isRecord(history)) return null;
  const archiveAvailable = data.archive_available;
  const choosing = archiveAvailable && entry !== "reconnect";
  const revision = data.expected_mix_revision;
  if (
    choosing &&
    (history.editable !== true ||
      typeof history.default !== "boolean" ||
      !Number.isSafeInteger(revision) ||
      (revision as number) < 0 ||
      data.preview_version !== PREVIEW_VERSION)
  ) {
    return null;
  }
  if (entry === "reconnect" && archiveAvailable && typeof history.saved_enabled !== "boolean") {
    return null;
  }
  let reset: ResetRow[] | null = null;
  if (choosing && isRecord(data.reset) && data.reset.applies === true) {
    if (!Array.isArray(data.reset.rows_after)) return null;
    const rows = data.reset.rows_after.map(readResetRow);
    if (rows.some((row) => row === null)) return null;
    reset = rows as ResetRow[];
  }
  return {
    archiveAvailable,
    entry,
    handle: bareHandle(data.handle),
    defaultOn: choosing && history.default === true,
    savedEnabled: typeof history.saved_enabled === "boolean" ? history.saved_enabled : null,
    expectedMixRevision: choosing ? (revision as number) : null,
    previewVersion: PREVIEW_VERSION,
    reset,
  };
}

/**
 * What the start posts. `{}` where past posts are no choice: the empty start,
 * byte for byte. `{"archive_choice": null}` for a reconnect, which keeps its
 * saved setting and must carry no choice. Otherwise the choice, with the
 * revision and version of the preview it was made on.
 */
export function startBody(preview: ConnectPreview, useHistory: boolean): Record<string, unknown> {
  if (!preview.archiveAvailable) return {};
  if (preview.entry === "reconnect") return { archive_choice: null };
  return {
    archive_choice: {
      use_history: useHistory,
      expected_mix_revision: preview.expectedMixRevision,
      preview_version: preview.previewVersion,
    },
  };
}

// ── The two doors and the flow over them ───────────────────────────────────

/** The connect a person pressed: the header's Add, or one row's. */
export type ConnectTarget = { workspaceId: string; accountId: string | null };

/**
 * The refusal for a preview this page could not read, which only the dialog's
 * refresh after a 409 can end in: the first preview that cannot be read starts
 * the connect with no choice (`beginConnect`).
 */
export const UNREADABLE_PREVIEW = "unreadable_preview";

type PreviewRead =
  | { ok: true; preview: ConnectPreview }
  | { ok: false; error: string; status: number };

async function readPreview({ workspaceId, accountId }: ConnectTarget): Promise<PreviewRead> {
  const query = accountId === null ? "" : `?account_id=${encodeURIComponent(accountId)}`;
  const result = await callBff(`/api/workspaces/${workspaceId}/accounts/connect-preview${query}`);
  if (!result.ok) return { ok: false, error: result.error, status: result.status };
  const preview = parseConnectPreview(result.data);
  return preview
    ? { ok: true, preview }
    : { ok: false, error: UNREADABLE_PREVIEW, status: result.status };
}

/** What the Accounts tab does next. */
export type ConnectStep =
  /** Leave for Instagram: the URL passed the authorize-host guard. */
  | { kind: "redirect"; url: string }
  /** Open (or keep open) the dialog over this preview, with a notice to say first. */
  | { kind: "ask"; preview: ConnectPreview; notice: string | null }
  | { kind: "refused"; error: string; status: number };

async function start(
  { workspaceId, accountId }: ConnectTarget,
  body: Record<string, unknown>,
): Promise<ConnectStep> {
  const result =
    accountId === null
      ? await requestWorkspaceConnect(workspaceId, body)
      : await requestDestinationConnect(workspaceId, accountId, body);
  return result.ok
    ? { kind: "redirect", url: result.authorizationUrl }
    : { kind: "refused", error: result.error, status: result.status };
}

/**
 * The header's Add or a row's Connect: preview first, then either ask (past
 * posts are a choice here) or start with no choice: where they are not, and
 * where the preview cannot be had, for whatever reason. The start then says
 * what is wrong with the connect, if anything is.
 */
export async function beginConnect(target: ConnectTarget): Promise<ConnectStep> {
  const read = await readPreview(target);
  if (read.ok && read.preview.archiveAvailable) {
    return { kind: "ask", preview: read.preview, notice: null };
  }
  return start(target, {});
}

/**
 * The dialog's Continue: start with the choice. A stale preview's 409 is
 * answered with a fresh preview and the conflict said in the dialog, never a
 * redirect. A fresh preview that no longer offers the choice ends the dialog
 * with the refusal that says so, and the next press starts with no choice. A
 * refresh that cannot be had refuses and keeps the dialog: unlike the first
 * preview, it follows a choice the person made, and the API has just shown it
 * has the feature, so a start with no choice would not be what they asked for.
 */
export async function confirmConnect(
  target: ConnectTarget,
  preview: ConnectPreview,
  useHistory: boolean,
): Promise<ConnectStep> {
  const step = await start(target, startBody(preview, useHistory));
  if (step.kind !== "refused" || step.error !== "mix_conflict") return step;
  const fresh = await readPreview(target);
  if (!fresh.ok) return { kind: "refused", error: fresh.error, status: fresh.status };
  if (!fresh.preview.archiveAvailable) {
    return { kind: "refused", error: "archive_not_activated", status: 503 };
  }
  return {
    kind: "ask",
    preview: fresh.preview,
    notice: sourceRefusalCopy("mix_conflict", 409, { surface: "connect" }),
  };
}

/**
 * The sentence for a refused connect. The past-posts reasons say themselves
 * (`source-refusals.ts`); everything else is the connect leg's own copy.
 */
export function connectRefusalCopy(error: string, status: number): string {
  if (error === UNREADABLE_PREVIEW) {
    return "Storydump could not read what connecting would do, so nothing was started. Reload the page and try again; if it repeats, this one is on us.";
  }
  if (Object.hasOwn(SOURCE_REFUSAL_COPY, error)) {
    return sourceRefusalCopy(error, status, { surface: "connect" });
  }
  return destinationConnectRefusalCopy(error);
}

// ── Words ──────────────────────────────────────────────────────────────────

export const HISTORY_INDEXING_COPY =
  "We index your past posts' details — no media is downloaded until a post is picked or previewed. Media is checked when used.";
export const ADD_EXISTING_COPY =
  "A new account uses this choice. An existing account keeps its source settings.";
export const PUBLISHING_ONLY_COPY =
  "Connect for publishing only: past posts stay Off, and you can turn them on later in Posting mix.";
export const RECONNECT_KEEPS_COPY = "Reconnect keeps this setting.";
export const RESET_TO_EQUAL_COPY =
  "Turning past posts on resets every share to equal, custom percentages included:";

/** How one row of the reset preview is named: its handle or folder, and what it is. */
export function resetRowName(row: ResetRow, preview: ConnectPreview): string {
  if (row.sourceId === null) {
    const handle = bareHandle(row.name) ?? preview.handle;
    return handle ? `@${handle}'s past posts (new)` : "This account's past posts (new)";
  }
  if (row.provider === "instagram_archive") {
    const handle = bareHandle(row.name);
    return handle ? `@${handle}'s past posts` : "Past posts";
  }
  const name = row.name ?? "Unnamed folder";
  return row.provider === "gdrive" ? `${name} · Google Drive` : name;
}

/** A share that is zero, written any way the API might write it. */
export function isZeroShare(share: string): boolean {
  return /^0+(\.0+)?$/.test(share);
}
