/**
 * The browser's one door to the command route (#1057/#1063, epic P3).
 *
 * P2 made the route capable of an entity-less command; this is what a control
 * calls. It exists so the identity rule below is held in ONE place rather than
 * re-derived at each call site — the epic's whole subject is a surface that
 * grew two dialects, and four Settings controls each minting their own key
 * would be that defect arriving one layer further in.
 *
 * ## The identity rule, and why it is structural rather than careful
 *
 * `submission_id` is minted HERE, per call, and cannot be supplied by the
 * caller. That is deliberate: F3 locked a client-generated UUID per submit,
 * and a caller-supplied id is one a component can hold in `useState` and reuse
 * across every save for the life of a mount. The failure that would cause is
 * the one F3 rejected a content hash to avoid, and it is invisible:
 *
 *   enhanced -> simple   key K, content simple   → applied
 *   simple   -> enhanced key K, content enhanced → 409, the port refuses it
 *   enhanced -> simple   key K, content simple   → 200 `replayed`, NOT executed
 *
 * The third call reports success and writes nothing. Minting inside the
 * function makes that sequence unrepresentable rather than merely discouraged.
 *
 * A retry of the SAME attempt would want the same id, which this does not yet
 * express — no caller retries, and an unused parameter is the seam through
 * which a reused id arrives. Add it with the caller that needs it.
 *
 * ## `replayed` is a failure here, not a success
 *
 * The port answers a same-key/same-body call with HTTP 200 and
 * `{"outcome":"replayed"}` — acknowledged, deliberately NOT executed
 * (`app.py:247-250`). For an intent command that is the correct, harmless
 * answer: the row already moved. For a settings write it is the F3 harm
 * exactly — "the user sees a success and the setting does not move, and the UI
 * takes the blame."
 *
 * With a fresh id per call it is unreachable. So it is reported as a failure
 * rather than smoothed over: if it ever arrives, the identity rule above has
 * broken, and the one thing the user must not be told is that their change
 * was saved.
 */

export type SubmitResult =
  | { ok: true; data: Record<string, unknown> }
  | { ok: false; error: string; status: number };

/**
 * The reason string for a `replayed` answer. Its own code, not folded into a
 * generic failure: it means the write did not happen AND the key mechanism is
 * wrong, which is a different remedy from a refusal by the port.
 */
export const REPLAYED_ERROR = "unexpected_replay";

/**
 * POST one command for a workspace, with a fresh submission identity.
 *
 * Returns a typed result rather than throwing. A thrown error would arrive at
 * a component's `catch` beside genuine network failures, and "the port refused
 * this value" and "the browser could not reach the app" want different
 * sentences.
 */
export async function submitCommand(
  workspaceId: string,
  command: string,
  body: Record<string, unknown> = {},
): Promise<SubmitResult> {
  let response: Response;
  try {
    response = await fetch(`/api/workspaces/${workspaceId}/commands/${command}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // The id rides in the body because that is where P2's `submissionCommand`
      // spec reads it; the route derives the header from it and the browser
      // never sets `Idempotency-Key` itself.
      body: JSON.stringify({ ...body, submission_id: crypto.randomUUID() }),
    });
  } catch {
    return { ok: false, error: "unreachable", status: 0 };
  }

  const data = await response.json().catch(() => ({}));

  if (!response.ok) {
    const error =
      typeof data?.error === "string"
        ? data.error
        : typeof data?.reason === "string"
          ? data.reason
          : `http_${response.status}`;
    return { ok: false, error, status: response.status };
  }

  if (data?.outcome === "replayed") {
    return { ok: false, error: REPLAYED_ERROR, status: response.status };
  }

  return { ok: true, data: data ?? {} };
}

/**
 * Submit a `settings_change`. The port owns which keys are legal and refuses
 * an unknown one BY NAME (`workspaces.py:466`), so nothing here re-states the
 * allowlist — a second copy is one that can disagree, and this is the copy
 * that would go stale.
 */
export function submitSettingsChange(
  workspaceId: string,
  settings: Record<string, unknown>,
): Promise<SubmitResult> {
  return submitCommand(workspaceId, "settings_change", { settings });
}

/**
 * A sentence for a settings refusal.
 *
 * Deliberately NOT `intents.ts`'s `refusalCopy`, which is not a shared table
 * that happens to be elsewhere — it is the QUEUE's vocabulary. Its reasons
 * (`illegal_transition`, `manual_mode`) cannot arise from a settings write and
 * these cannot arise from a queue action, so folding them together would put
 * "This post is no longer in the queue" on a form about posting hours. Two
 * disjoint vocabularies, not two copies of one decision.
 */
/** Rename the workspace. `submitCommand` mints the submission id. */
export function submitRenameWorkspace(workspaceId: string, name: string) {
  return submitCommand(workspaceId, "rename_workspace", { name });
}

export function settingsRefusalCopy(reason: unknown, status?: number): string {
  // A permission refusal carries NO reason to switch on. `insufficient_role`
  // is mapped to 403 and answered `{detail: "forbidden"}` — no `error`, no
  // `reason` — so `submitCommand` synthesises `http_403` and every branch below
  // misses. Without this, a member refused for their ROLE and a browser that
  // could not reach the app got the same sentence, and the remedy for one is
  // "ask an admin" while the remedy for the other is "try again".
  //
  // Keyed on the STATUS rather than the synthesised string, because the status
  // is the real signal and `http_403` is only a stand-in for it. On this path
  // 403 has exactly one cause: a non-member is answered 404, never 403
  // (`v1.py:9`, deliberate non-disclosure), so a 403 means the caller IS a
  // member and their role is too low.
  if (status === 403) {
    return "You do not have permission to change this. An admin or the workspace owner can.";
  }

  switch (reason) {
    case REPLAYED_ERROR:
      // Not smoothed into the generic line: this one means the change did NOT
      // land, which the generic line also says, AND that the key mechanism is
      // broken, which someone needs to see.
      return "That change was not saved — the app sent it under a key the server had already seen. Reload and try again; report it if it repeats.";
    case "invalid_settings":
      return "Nothing to save — no setting changed.";
    case "invalid_name":
      return "Give the workspace a name.";
    case "unauthenticated":
      return "That session expired. Sign in again.";
    case "unreachable":
    case "target_router_unreachable":
      return "Storydump cannot reach the server right now. Nothing changed — try again shortly.";
  }
  return "That did not save. Nothing changed — try again shortly.";
}
