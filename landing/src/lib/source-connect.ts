/**
 * The Drive connect leg, browser side and its one shared guard (#1065).
 *
 * Separate from `command-client.ts` on purpose: that module exists to mint a
 * per-submission identity and derive an `Idempotency-Key`, and neither applies
 * here. An OAuth leg is a browser redirect the command port cannot express, so
 * the API serves it as a resource route; and minting a state is deliberately
 * last-issued-wins, so a second click must retire the first rather than replay
 * it. Routing this through the command client would have to disable both of
 * its reasons for existing.
 */

/** The one host the browser may be sent to by this flow. */
const AUTHORIZATION_HOST = "accounts.google.com";

/**
 * HTTPS, and Google's account host exactly.
 *
 * The value comes from the API, which builds it from a fixed constant
 * (`google_oidc.AUTHORIZE_URL`), so today this can only pass. It exists
 * because the response is NAVIGATED TO: the check costs one URL parse, and
 * without it a future upstream change turns the proxy into an open redirect
 * with nothing to notice. A guard on a redirect target is worth having before
 * it is needed rather than after.
 */
export function isAuthorizationUrl(value: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  // EQUALITY, not `endsWith` — which `evil-accounts.google.com` and
  // `accounts.google.com.evil.example` both satisfy. That is the real trap
  // here and it is what this line exists for.
  //
  // Against `host` rather than `hostname`, which is the stricter of the two:
  // `host` carries a non-default port, so `https://accounts.google.com:1234`
  // is refused, while `hostname` strips it and would accept. A port variant is
  // not a threat we could actually be exposed to — reaching it means holding
  // Google's own host — but refusing it costs nothing and leaves one fewer
  // shape to reason about. The default port is omitted from `host`, so the
  // ordinary URL passes.
  return parsed.protocol === "https:" && parsed.host === AUTHORIZATION_HOST;
}

export type ConnectResult =
  | { ok: true; authorizationUrl: string }
  | { ok: false; error: string; status: number };

/**
 * Ask for the authorization URL for ONE source. Returns it rather than
 * navigating: the caller owns the navigation, so a component can render a
 * refusal instead of leaving the person on a page that silently did nothing.
 */
export async function requestSourceConnect(
  workspaceId: string,
  sourceId: string,
): Promise<ConnectResult> {
  let response: Response;
  try {
    response = await fetch(
      `/api/workspaces/${workspaceId}/sources/${sourceId}/connect`,
      { method: "POST", headers: { "Content-Type": "application/json" } },
    );
  } catch {
    return { ok: false, error: "unreachable", status: 0 };
  }

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = typeof data?.error === "string" ? data.error : `http_${response.status}`;
    return { ok: false, error, status: response.status };
  }

  const url = data?.authorizationUrl;
  // Checked again on the client, deliberately. The route already guarantees
  // it, and this is the line immediately before `window.location.assign` —
  // the guard belongs where the navigation is, not only where the value was
  // fetched, because those two can drift apart.
  if (typeof url !== "string" || !isAuthorizationUrl(url)) {
    return { ok: false, error: "malformed_authorization_url", status: response.status };
  }
  return { ok: true, authorizationUrl: url };
}

/** A sentence for a connect refusal. */
export function connectRefusalCopy(reason: unknown): string {
  switch (reason) {
    case "not found":
    case "http_404":
      return "That source is no longer here. Reload the page.";
    case "unauthenticated":
    case "http_401":
      return "That session expired. Sign in again.";
    case "malformed_authorization_url":
      return "Storydump could not start the Google sign-in. Nothing changed — report this if it repeats.";
    case "unreachable":
    case "target_router_unreachable":
      return "Storydump cannot reach the server right now. Nothing changed — try again shortly.";
  }
  return "Could not start the Google sign-in. Nothing changed — try again shortly.";
}

export type AddSourceResult =
  | { ok: true; sourceId: string; created: boolean }
  | { ok: false; error: string; status: number };

/**
 * Add a Drive folder as a source.
 *
 * REST rather than a command, per F1 (b): a folder is a resource and the
 * vocabulary has no name for creating one. No submission id — the target route
 * is idempotent on the folder under an advisory lock, so a repeat returns the
 * SAME source rather than a second one, and `created` says which happened.
 */
export async function addDriveSource(
  workspaceId: string,
  folderRef: string,
  rootName?: string,
): Promise<AddSourceResult> {
  let response: Response;
  try {
    response = await fetch(`/api/workspaces/${workspaceId}/sources`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(
        rootName?.trim()
          ? { folder_ref: folderRef, root_name: rootName.trim() }
          : { folder_ref: folderRef },
      ),
    });
  } catch {
    return { ok: false, error: "unreachable", status: 0 };
  }

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = typeof data?.error === "string" ? data.error : `http_${response.status}`;
    return { ok: false, error, status: response.status };
  }
  if (typeof data?.sourceId !== "string") {
    return { ok: false, error: "malformed_response", status: response.status };
  }
  return { ok: true, sourceId: data.sourceId, created: data.created === true };
}

/** A sentence for a failed folder add. */
export function addSourceRefusalCopy(reason: unknown): string {
  switch (reason) {
    case "folder_required":
      return "Paste a Google Drive folder link or its folder id.";
    case "invalid_args":
      // The port's own refusal for a value `folder_ref_from` will not accept —
      // a link that is not a folder link is refused rather than salvaged.
      return "That does not look like a Drive folder link. Open the folder in Drive and copy the address.";
    case "unauthenticated":
    case "http_401":
      return "That session expired. Sign in again.";
    case "unreachable":
    case "target_router_unreachable":
      return "Storydump cannot reach the server right now. Nothing was added — try again shortly.";
  }
  return "Could not add that folder. Nothing was added — try again shortly.";
}
