import { notAuthenticatedCopy, unreachableCopy } from "./refusal-copy";
import { isHttpsUrlOnHost } from "./redirect-guard";
import { requestGrant } from "./start-grant";
import type { GrantResult } from "./start-grant";
import type { DriveStatus } from "./dashboard-payloads";

/**
 * Google Drive, browser side — the WORKSPACE's grant and the folders picked
 * under it (owner ruling 2026-09-05, #1165 lean (b); `07` §15).
 *
 * Connect once, then pick folders from a browser of that Drive. The grant
 * is one per workspace; the same Google account connected from another
 * workspace is a second grant held there, and the same folder may be picked
 * in more than one workspace. Separate from `command-client.ts` on purpose:
 * an OAuth leg is a browser redirect the command port cannot express, and
 * a folder is a resource (F1 (b)), so both are REST at the proxy.
 */

/** The one host the browser may be sent to by this flow. */
const AUTHORIZATION_HOST = "accounts.google.com";

/**
 * HTTPS, and Google's account host exactly. The value comes from the API,
 * which builds it from a fixed constant, so today this can only pass; it
 * exists because the response is NAVIGATED TO, and a guard on a redirect
 * target is worth having before it is needed rather than after.
 */
export function isGoogleAuthorizationUrl(value: string): boolean {
  return isHttpsUrlOnHost(value, AUTHORIZATION_HOST);
}

export type DriveConnectResult = GrantResult;

/** Start (or restart) the WORKSPACE's Drive grant: the browser goes to Google. */
export function requestDriveConnect(
  workspaceId: string,
): Promise<DriveConnectResult> {
  return requestGrant(
    `/api/workspaces/${workspaceId}/drive/connect`,
    isGoogleAuthorizationUrl,
  );
}

export function driveConnectRefusalCopy(reason: unknown): string {
  switch (reason) {
    case "unauthenticated":
    case "http_401":
      return notAuthenticatedCopy("Nothing changed.");
    case "http_403":
    case "insufficient_role":
      return "You need to be an admin of this workspace to connect Google Drive.";
    case "malformed_authorization_url":
      return "Storydump could not start the Google sign-in. Nothing changed — report this if it repeats.";
    case "unreachable":
    case "target_router_unreachable":
      return unreachableCopy("Nothing changed");
  }
  return "Could not start the Google sign-in. Nothing changed — try again shortly.";
}

export type DriveBadge = {
  label: string;
  tone: "active" | "attention" | "inert";
};

/**
 * A `Record` keyed on the closed set rather than a `switch`, so a status
 * without a badge is a COMPILE error. The invariant, pinned by a test: only
 * `active` is ever green. `expired` and `revoked` share a tone because they
 * share a remedy, and keep distinct labels because they do not share a cause.
 */
const DRIVE_BADGE: Record<
  "none" | "active" | "expired" | "revoked",
  DriveBadge
> = {
  none: { label: "Not connected", tone: "attention" },
  active: { label: "Connected", tone: "active" },
  expired: { label: "Access expired — reconnect", tone: "attention" },
  revoked: { label: "Access revoked — reconnect", tone: "attention" },
};

export function driveStatusBadge(
  status: string | null | undefined,
): DriveBadge {
  return (
    DRIVE_BADGE[status as keyof typeof DRIVE_BADGE] ?? {
      label: "Connection status unknown",
      tone: "inert",
    }
  );
}

export type DriveConnectControl = {
  label: string;
  kind: "connect" | "reconnect";
};

/** Connect before a grant, Reconnect after a dead one, nothing for a live one. */
export function driveConnectControl(
  status: string | null | undefined,
): DriveConnectControl | null {
  switch (status) {
    case "none":
      return { label: "Connect Google Drive", kind: "connect" };
    case "expired":
    case "revoked":
      return { label: "Reconnect Google Drive", kind: "reconnect" };
    case "active":
      // Offered while live too: Google can revoke a grant on its side without
      // the projection knowing until the next call, and Disconnect → Connect
      // is a worse road back (review of #1246).
      return { label: "Reconnect Google Drive", kind: "reconnect" };
  }
  return { label: "Connect Google Drive", kind: "connect" };
}

/** The picker's second root — the folders shared TO the connected account. */
export const SHARED_ROOT = "shared-with-me";

export type DriveFolder = { id: string; name: string };

export type FoldersResult =
  | { ok: true; parent: string; folders: DriveFolder[]; truncated: boolean }
  | { ok: false; error: string; status: number };

/** The folders under `parent` (the Drive root when null), through the grant. */
export async function fetchDriveFolders(
  workspaceId: string,
  parent: string | null,
): Promise<FoldersResult> {
  const query = parent ? `?parent=${encodeURIComponent(parent)}` : "";
  let response: Response;
  try {
    response = await fetch(
      `/api/workspaces/${workspaceId}/drive/folders${query}`,
    );
  } catch {
    return { ok: false, error: "unreachable", status: 0 };
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error =
      typeof data?.error === "string" ? data.error : `http_${response.status}`;
    return { ok: false, error, status: response.status };
  }
  if (!Array.isArray(data?.folders)) {
    return { ok: false, error: "malformed_response", status: response.status };
  }
  const folders = (data.folders as unknown[]).flatMap((f) => {
    const row = f as { id?: unknown; name?: unknown };
    return typeof row?.id === "string" && typeof row?.name === "string"
      ? [{ id: row.id, name: row.name }]
      : [];
  });
  return {
    ok: true,
    parent: typeof data?.parent === "string" ? data.parent : "root",
    folders,
    truncated: data?.truncated === true,
  };
}

export function driveFoldersRefusalCopy(reason: unknown): string {
  switch (reason) {
    case "drive_not_connected":
      return "Google Drive is not connected to this workspace yet. Connect it first.";
    case "drive_reconnect_needed":
      return "Google no longer accepts this workspace's Drive access. Reconnect Google Drive to browse.";
    case "drive_grant_refused":
      return "Google refused the Drive access this workspace holds. Reconnect Google Drive to browse.";
    case "drive_unavailable":
      return "Google did not answer just now. Try again in a moment.";
    case "invalid_parent":
      return "That folder could not be opened.";
    case "unauthenticated":
    case "http_401":
      return notAuthenticatedCopy("Nothing changed.");
    case "http_403":
      return "You need to be an admin of this workspace to browse its Drive.";
    case "unreachable":
    case "target_router_unreachable":
      return unreachableCopy("Nothing changed");
  }
  return "Could not list your Drive folders. Try again shortly.";
}

export type AddFolderResult =
  | { ok: true; sourceId: string; created: boolean }
  | { ok: false; error: string; status: number };

/**
 * Pick a folder under the grant. REST rather than a command (F1 (b)); no
 * submission id — the route is idempotent on the folder, so a repeat returns
 * the SAME source, revived if it had been removed, and `created` says which.
 */
export async function addDriveFolder(
  workspaceId: string,
  folder: DriveFolder,
): Promise<AddFolderResult> {
  let response: Response;
  try {
    response = await fetch(`/api/workspaces/${workspaceId}/sources`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder_ref: folder.id, folder_name: folder.name }),
    });
  } catch {
    return { ok: false, error: "unreachable", status: 0 };
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error =
      typeof data?.error === "string" ? data.error : `http_${response.status}`;
    return { ok: false, error, status: response.status };
  }
  if (typeof data?.sourceId !== "string") {
    return { ok: false, error: "malformed_response", status: response.status };
  }
  return { ok: true, sourceId: data.sourceId, created: data.created === true };
}

export function addFolderRefusalCopy(reason: unknown): string {
  switch (reason) {
    case "drive_not_connected":
      return "Google Drive is not connected to this workspace yet. Connect it first. Nothing was added.";
    case "invalid_args":
    case "folder_required":
      return "That does not look like a Drive folder. Nothing was added.";
    case "unauthenticated":
    case "http_401":
      return notAuthenticatedCopy("Nothing was added.");
    case "unreachable":
    case "target_router_unreachable":
      return unreachableCopy("Nothing was added");
  }
  return "Could not add that folder. Nothing was added — try again shortly.";
}

export type RemoveFolderResult =
  { ok: true } | { ok: false; error: string; status: number };

/** Remove a folder from the sync — a pause on the API side, never a delete. */
export async function removeDriveFolder(
  workspaceId: string,
  sourceId: string,
): Promise<RemoveFolderResult> {
  let response: Response;
  try {
    response = await fetch(
      `/api/workspaces/${workspaceId}/sources/${sourceId}`,
      {
        method: "DELETE",
      },
    );
  } catch {
    return { ok: false, error: "unreachable", status: 0 };
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error =
      typeof data?.error === "string" ? data.error : `http_${response.status}`;
    return { ok: false, error, status: response.status };
  }
  return { ok: true };
}

export function removeFolderRefusalCopy(reason: unknown): string {
  switch (reason) {
    case "not found":
    case "http_404":
      return "That folder is no longer here. Reload the page.";
    case "unauthenticated":
    case "http_401":
      return notAuthenticatedCopy("Nothing changed.");
    case "unreachable":
    case "target_router_unreachable":
      return unreachableCopy("Nothing changed");
  }
  return "Could not remove that folder. Nothing changed — try again shortly.";
}

/** "Connected since" for the card, from the grant's `connected_at`. */
export function driveConnectedSince(drive: DriveStatus | null): string | null {
  if (!drive?.connected_at) return null;
  const at = new Date(drive.connected_at);
  return Number.isNaN(at.getTime()) ? null : at.toLocaleDateString();
}
