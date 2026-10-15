import { sourceRefusalCopy } from "./source-refusals";
import {
  editDraft,
  enabledSharesLine,
  newerMix,
  openDraft,
  rebaseDraft,
  resetExplanationCopy,
  sourceAccessibleName,
  type ChangeAction,
  type CustomDraft,
  type MixAction,
  type MixCallResult,
  type SourceMix,
} from "./source-mix";

/**
 * The posting-mix card's states, and what moves it between them, as one
 * reducer the card dispatches to. `vitest.config.ts` runs in node, so no test
 * presses a button; the decisions live here, where a test can drive them.
 *
 * Two ways to change a mix, never open at once:
 *   - a CHANGE (Reset to equal, or a source's On/Off) resets every share to
 *     equal. It is previewed first, with the same body sent to the preview
 *     route, and made only on Confirm;
 *   - a DRAFT of custom percentages is made only on Save.
 *
 * A 409 `mix_conflict` keeps whichever was open. The draft stays, typed text
 * and all, against the revision it was opened at, and the card reloads the mix
 * (`router.refresh()`). While the draft's revision is not the mix's, it cannot
 * be saved: it is shown beside the current mix with its rows marked, until the
 * person carries it over or drops it. A change goes stale and is previewed
 * again. Nothing here replays a stale write.
 */

export type ChangePhase = "previewing" | "ready" | "saving" | "stale";

export type PendingChange = {
  action: ChangeAction;
  phase: ChangePhase;
  /** The would-be mix the preview answered; null until it has. */
  preview: SourceMix | null;
};

export type MixFlow = {
  /** The newest mix a save answered. The page's read can be older for a moment. */
  local: SourceMix | null;
  draft: CustomDraft | null;
  /** A custom save is in flight. */
  savingDraft: boolean;
  pending: PendingChange | null;
  /** The revision a conflict named. The card waits for a mix at least this new. */
  conflictRevision: number | null;
  /** A refusal, as copy. Shown as an alert. */
  error: string | null;
  /** What the polite live region says: previews, saves and conflicts. */
  announcement: string;
};

export const INITIAL_FLOW: MixFlow = {
  local: null,
  draft: null,
  savingDraft: false,
  pending: null,
  conflictRevision: null,
  error: null,
  announcement: "",
};

export type MixFlowEvent =
  | { type: "draft_opened"; mix: SourceMix }
  | { type: "draft_edited"; sourceId: string; text: string }
  | { type: "draft_rebased"; mix: SourceMix }
  | { type: "draft_discarded" }
  | { type: "draft_save_started" }
  | { type: "change_requested"; action: ChangeAction }
  | { type: "change_confirmed" }
  | { type: "change_cancelled" }
  | { type: "preview_answered"; action: ChangeAction; result: MixCallResult }
  | { type: "save_answered"; action: MixAction; result: MixCallResult };

export function isMixConflict(result: MixCallResult): boolean {
  return !result.ok && result.error === "mix_conflict";
}

/** The current mix: the page's read or the card's last answer, whichever is newer. */
export function currentMix(page: SourceMix | null, flow: MixFlow): SourceMix | null {
  return newerMix(page, flow.local);
}

/**
 * Whether a change may be previewed now. An open change that is not in flight
 * is replaced by the fresh preview ("Preview again"); one being previewed or
 * saved, or an open draft, waits. The card asks before it sends anything.
 */
export function canRequestChange(flow: MixFlow): boolean {
  if (flow.draft) return false;
  return !flow.pending || (flow.pending.phase !== "previewing" && flow.pending.phase !== "saving");
}

/** Whether the card is still waiting for the mix a conflict named. */
export function awaitingReload(flow: MixFlow, current: SourceMix | null): boolean {
  return flow.conflictRevision !== null && (!current || current.revision < flow.conflictRevision);
}

/** Whether the open draft was made against another mix than the one shown. */
export function draftIsStale(flow: MixFlow, current: SourceMix | null): boolean {
  return flow.draft !== null && current !== null && flow.draft.basedOn !== current.revision;
}

/** Whether the open change was previewed against another mix than the one shown. */
export function changeIsStale(flow: MixFlow, current: SourceMix | null): boolean {
  const pending = flow.pending;
  if (!pending) return false;
  return pending.phase === "stale" || current === null || pending.action.expected_revision !== current.revision;
}

/** The shares a would-be mix gives the sources that are on, as one line. */
export function sharesSummary(mix: SourceMix): string {
  return enabledSharesLine(mix) ?? "no source would be on, so nothing would be selected for posting";
}

function changeLabel(action: ChangeAction, mix: SourceMix): string {
  if (action.action === "reset_equal") return "Resetting to equal";
  const row = mix.rows.find((r) => r.sourceId === action.source_id);
  const name = row ? sourceAccessibleName(row) : "this source";
  return `Turning ${name} ${action.enabled ? "on" : "off"}`;
}

const DRAFT_CONFLICT =
  "The posting mix changed while you were editing. Your percentages are kept; review them against the current mix before saving.";
const CHANGE_CONFLICT =
  "The posting mix changed before this was saved. Preview the change again against the current mix.";

function refused(result: Extract<MixCallResult, { ok: false }>): string {
  return sourceRefusalCopy(result.error, result.status, { surface: "mix" });
}

/**
 * The revision to wait for after a conflict. A 409 normally names it; one that
 * does not still means the draft's revision is gone, so wait for a later one.
 */
function conflictAt(result: Extract<MixCallResult, { ok: false }>, basedOn: number): number {
  return result.currentRevision ?? basedOn + 1;
}

export function mixFlowReducer(flow: MixFlow, event: MixFlowEvent): MixFlow {
  switch (event.type) {
    case "draft_opened":
      if (flow.draft || flow.pending) return flow;
      return { ...flow, draft: openDraft(event.mix), error: null, announcement: "" };

    case "draft_edited":
      if (!flow.draft || flow.savingDraft) return flow;
      return { ...flow, draft: editDraft(flow.draft, event.sourceId, event.text) };

    case "draft_rebased":
      if (!flow.draft || flow.savingDraft) return flow;
      return {
        ...flow,
        draft: rebaseDraft(flow.draft, event.mix),
        conflictRevision: null,
        error: null,
        announcement:
          "Your percentages are now on the current mix. Check that they add up to 100%, then save.",
      };

    case "draft_discarded":
      if (flow.savingDraft) return flow;
      return { ...flow, draft: null, conflictRevision: null, error: null, announcement: "" };

    case "draft_save_started":
      if (!flow.draft || flow.savingDraft) return flow;
      return { ...flow, savingDraft: true, error: null, announcement: "" };

    case "change_requested":
      if (!canRequestChange(flow)) return flow;
      return {
        ...flow,
        pending: { action: event.action, phase: "previewing", preview: null },
        conflictRevision: null,
        error: null,
        announcement: "",
      };

    case "change_cancelled":
      if (!flow.pending || flow.pending.phase === "saving") return flow;
      return { ...flow, pending: null, conflictRevision: null, error: null, announcement: "" };

    case "change_confirmed":
      if (!flow.pending || flow.pending.phase !== "ready") return flow;
      return { ...flow, pending: { ...flow.pending, phase: "saving" }, error: null };

    case "preview_answered": {
      // An answer for a change the person has since cancelled or replaced is not this one's.
      const pending = flow.pending;
      if (!pending || pending.action !== event.action || pending.phase !== "previewing") return flow;
      const { result } = event;
      if (result.ok) {
        return {
          ...flow,
          pending: { ...pending, phase: "ready", preview: result.mix },
          announcement: `${changeLabel(event.action, result.mix)} would make the shares: ${sharesSummary(result.mix)}. Confirm or cancel.`,
        };
      }
      if (isMixConflict(result)) {
        return {
          ...flow,
          pending: { ...pending, phase: "stale" },
          conflictRevision: conflictAt(result, event.action.expected_revision),
          announcement: CHANGE_CONFLICT,
        };
      }
      return { ...flow, pending: null, error: refused(result), announcement: "" };
    }

    case "save_answered": {
      const { action, result } = event;
      if (action.action === "save_custom") {
        if (!flow.savingDraft || !flow.draft) return flow;
        if (result.ok) {
          return {
            ...flow,
            local: newerMix(flow.local, result.mix),
            draft: null,
            savingDraft: false,
            conflictRevision: null,
            error: null,
            announcement: "Posting mix saved: custom percentages. They apply from the next posting slot.",
          };
        }
        if (isMixConflict(result)) {
          // The draft stays exactly as typed, against the revision it was opened at.
          return {
            ...flow,
            savingDraft: false,
            conflictRevision: conflictAt(result, flow.draft.basedOn),
            error: null,
            announcement: DRAFT_CONFLICT,
          };
        }
        return { ...flow, savingDraft: false, error: refused(result), announcement: "" };
      }
      const pending = flow.pending;
      if (!pending || pending.action !== action || pending.phase !== "saving") return flow;
      if (result.ok) {
        return {
          ...flow,
          local: newerMix(flow.local, result.mix),
          pending: null,
          conflictRevision: null,
          error: null,
          announcement: `Posting mix saved. ${resetExplanationCopy(result.mix.resetExplanation) ?? "Every share is as shown."}`,
        };
      }
      if (isMixConflict(result)) {
        return {
          ...flow,
          pending: { ...pending, phase: "stale" },
          conflictRevision: conflictAt(result, action.expected_revision),
          error: null,
          announcement: CHANGE_CONFLICT,
        };
      }
      // Refused for another reason: the preview stays, to retry or cancel.
      return { ...flow, pending: { ...pending, phase: "ready" }, error: refused(result), announcement: "" };
    }
  }
}
