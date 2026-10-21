import { describe, expect, it } from "vitest";
import {
  editDraft,
  parseSourceMix,
  resetEqualAction,
  saveCustomAction,
  setEnabledAction,
  type MixCallResult,
  type SourceMix,
} from "./source-mix";
import {
  INITIAL_FLOW,
  awaitingReload,
  canRequestChange,
  changeIsStale,
  currentMix,
  draftIsStale,
  mixFlowReducer,
  sharesSummary,
  type MixFlow,
  type MixFlowEvent,
} from "./source-mix-flow";
import { SOURCE_REFUSAL_COPY } from "./source-refusals";

/**
 * The card's two paths through the reducer it dispatches to (no DOM here, so
 * the decisions are driven directly): a change previewed, then confirmed; and
 * a custom draft that meets a conflict and survives it.
 */

const IG = "22222222-2222-4222-8222-222222222222";
const DRIVE = "33333333-3333-4333-8333-333333333333";
const NEW = "88888888-8888-4888-8888-888888888888";

function mix(revision: number, over: Record<string, unknown> = {}): SourceMix {
  const parsed = parseSourceMix({
    protocol_version: 2,
    revision,
    mode: "equal",
    supported_actions: ["reset_equal", "set_enabled", "save_custom"],
    rows: [
      {
        source_id: IG,
        provider: "instagram_past_posts",
        origin_handle: "example_handle",
        enabled: true,
        ratio: "0.5",
        target_share: "0.5",
      },
      { source_id: DRIVE, provider: "gdrive", name: "Event photos", enabled: true, ratio: "0.5", target_share: "0.5" },
    ],
    destinations: [],
    past_posts_available: true,
    ...over,
  });
  if (!parsed) throw new Error("fixture did not parse");
  return parsed;
}

const ok = (m: SourceMix): MixCallResult => ({ ok: true, mix: m });
const conflict = (current: number | null): MixCallResult => ({
  ok: false,
  error: "mix_conflict",
  status: 409,
  currentRevision: current,
});

function run(events: MixFlowEvent[], from: MixFlow = INITIAL_FLOW): MixFlow {
  return events.reduce(mixFlowReducer, from);
}

/** Instagram alone after Drive is turned off. */
const DRIVE_OFF = (revision: number) =>
  mix(revision, {
    reset_explanation: "enabled_membership_changed",
    rows: [
      {
        source_id: IG,
        provider: "instagram_past_posts",
        origin_handle: "example_handle",
        enabled: true,
        ratio: "1",
        target_share: "1",
      },
      { source_id: DRIVE, provider: "gdrive", name: "Event photos", enabled: false, ratio: "0", target_share: "0" },
    ],
  });

describe("a change is previewed, then made only on Confirm", () => {
  const page = mix(8);
  const off = setEnabledAction(8, DRIVE, false);

  it("previews, shows the would-be shares and says them, and makes nothing yet", () => {
    const flow = run([
      { type: "change_requested", action: off },
      { type: "preview_answered", action: off, result: ok(DRIVE_OFF(8)) },
    ]);
    expect(flow.pending?.phase).toBe("ready");
    expect(flow.pending?.preview?.rows.map((r) => r.targetShare)).toEqual(["1", "0"]);
    expect(flow.local).toBeNull();
    expect(flow.announcement).toBe(
      "Turning Event photos (Google Drive) off would make the shares: Instagram past posts @example_handle 100%. Confirm or cancel.",
    );
  });

  it("makes the change on Confirm, with the same action, and says what the reset did", () => {
    const flow = run([
      { type: "change_requested", action: off },
      { type: "preview_answered", action: off, result: ok(DRIVE_OFF(8)) },
      { type: "change_confirmed" },
    ]);
    expect(flow.pending?.phase).toBe("saving");
    const done = mixFlowReducer(flow, { type: "save_answered", action: off, result: ok(DRIVE_OFF(9)) });
    expect(done.pending).toBeNull();
    expect(done.local?.revision).toBe(9);
    expect(currentMix(page, done)?.revision).toBe(9);
    expect(done.announcement).toBe(
      "Posting mix saved. Which sources are on changed, so every share was reset to equal.",
    );
  });

  it("drops a preview answer for a change since cancelled", () => {
    const flow = run([
      { type: "change_requested", action: off },
      { type: "change_cancelled" },
      { type: "preview_answered", action: off, result: ok(DRIVE_OFF(8)) },
    ]);
    expect(flow.pending).toBeNull();
    expect(flow.announcement).toBe("");
  });

  it("will not confirm before the preview has answered", () => {
    const flow = run([{ type: "change_requested", action: off }, { type: "change_confirmed" }]);
    expect(flow.pending?.phase).toBe("previewing");
  });

  it("says every source would be off when the last one is turned off", () => {
    const allOff = mix(8, {
      rows: [
        { source_id: IG, provider: "instagram_past_posts", enabled: false, ratio: "0", target_share: "0" },
        { source_id: DRIVE, provider: "gdrive", name: "Event photos", enabled: false, ratio: "0", target_share: "0" },
      ],
    });
    expect(sharesSummary(allOff)).toBe(
      "no source would be on, so nothing would be selected for posting",
    );
  });

  it("keeps the preview after a refusal that is not a conflict, to retry or cancel", () => {
    const flow = run([
      { type: "change_requested", action: off },
      { type: "preview_answered", action: off, result: ok(DRIVE_OFF(8)) },
      { type: "change_confirmed" },
      {
        type: "save_answered",
        action: off,
        result: { ok: false, error: "past_posts_not_activated", status: 503, currentRevision: null },
      },
    ]);
    expect(flow.pending?.phase).toBe("ready");
    expect(flow.error).toBe(SOURCE_REFUSAL_COPY.past_posts_not_activated);
  });

  it("drops the change when the preview itself is refused, and says why", () => {
    const flow = run([
      { type: "change_requested", action: off },
      {
        type: "preview_answered",
        action: off,
        result: { ok: false, error: "mix_protocol_required", status: 400, currentRevision: null },
      },
    ]);
    expect(flow.pending).toBeNull();
    expect(flow.error).toBe(SOURCE_REFUSAL_COPY.mix_protocol_required);
  });
});

describe("a change that meets a conflict is previewed again, never replayed", () => {
  const reset = resetEqualAction(8);

  it("goes stale on a conflict at Confirm, and waits for the mix the conflict named", () => {
    const flow = run([
      { type: "change_requested", action: reset },
      { type: "preview_answered", action: reset, result: ok(mix(8)) },
      { type: "change_confirmed" },
      { type: "save_answered", action: reset, result: conflict(10) },
    ]);
    expect(flow.pending?.phase).toBe("stale");
    expect(flow.conflictRevision).toBe(10);
    expect(flow.announcement).toBe(
      "The posting mix changed before this was saved. Preview the change again against the current mix.",
    );
    // The page has not read revision 10 yet.
    expect(awaitingReload(flow, mix(8))).toBe(true);
    // It has now; the change is still stale until previewed again.
    expect(awaitingReload(flow, mix(10))).toBe(false);
    expect(changeIsStale(flow, mix(10))).toBe(true);
    expect(canRequestChange(flow)).toBe(true);
  });

  it("is replaced by a fresh preview against the current revision", () => {
    const stale = run([
      { type: "change_requested", action: reset },
      { type: "preview_answered", action: reset, result: conflict(10) },
    ]);
    expect(stale.pending?.phase).toBe("stale");
    const again = resetEqualAction(10);
    const fresh = run(
      [
        { type: "change_requested", action: again },
        { type: "preview_answered", action: again, result: ok(mix(10)) },
      ],
      stale,
    );
    expect(fresh.pending?.action).toBe(again);
    expect(fresh.pending?.phase).toBe("ready");
    expect(fresh.conflictRevision).toBeNull();
    expect(changeIsStale(fresh, mix(10))).toBe(false);
  });

  it("goes stale when the page reads a newer mix while the preview waits on Confirm", () => {
    const flow = run([
      { type: "change_requested", action: reset },
      { type: "preview_answered", action: reset, result: ok(mix(8)) },
    ]);
    expect(changeIsStale(flow, mix(8))).toBe(false);
    expect(changeIsStale(flow, mix(9))).toBe(true);
  });

  it("waits for a later revision when a conflict names none", () => {
    const flow = run([
      { type: "change_requested", action: reset },
      { type: "preview_answered", action: reset, result: conflict(null) },
    ]);
    expect(flow.conflictRevision).toBe(9);
  });
});

describe("a custom draft survives a conflict", () => {
  const page8 = mix(8, { mode: "custom" });
  const opened = run([{ type: "draft_opened", mix: page8 }]);
  const typed = run(
    [
      { type: "draft_edited", sourceId: IG, text: "70" },
      { type: "draft_edited", sourceId: DRIVE, text: "30" },
    ],
    opened,
  );
  const save = saveCustomAction(8, [
    { source_id: IG, ratio: "0.7" },
    { source_id: DRIVE, ratio: "0.3" },
  ]);
  const refused = run(
    [
      { type: "draft_save_started" },
      { type: "save_answered", action: save, result: conflict(9) },
    ],
    typed,
  );

  it("keeps the draft exactly as typed, against the revision it was opened at", () => {
    expect(refused.draft).toBe(typed.draft);
    expect(refused.draft?.basedOn).toBe(8);
    expect(refused.draft?.rows.map((r) => r.percent)).toEqual(["70", "30"]);
    expect(refused.savingDraft).toBe(false);
    expect(refused.conflictRevision).toBe(9);
    expect(refused.error).toBeNull();
    expect(refused.announcement).toBe(
      "The posting mix changed while you were editing. Your percentages are kept; review them against the current mix before saving.",
    );
  });

  it("cannot be saved while it waits for the current mix, nor once it is behind it", () => {
    // Before the refresh lands, the page still shows revision 8.
    expect(awaitingReload(refused, page8)).toBe(true);
    // After it, the draft is behind the mix shown.
    const page9 = mix(9, {
      rows: [
        { source_id: IG, provider: "instagram_past_posts", origin_handle: "example_handle", enabled: true, ratio: "0.5", target_share: "0.5" },
        { source_id: NEW, provider: "gdrive", name: "New folder", enabled: true, ratio: "0.5", target_share: "0.5" },
      ],
    });
    expect(awaitingReload(refused, page9)).toBe(false);
    expect(draftIsStale(refused, page9)).toBe(true);
  });

  it("is carried over only by choice, onto the mix as it is now", () => {
    const page9 = mix(9, {
      rows: [
        { source_id: IG, provider: "instagram_past_posts", origin_handle: "example_handle", enabled: true, ratio: "0.5", target_share: "0.5" },
        { source_id: NEW, provider: "gdrive", name: "New folder", enabled: true, ratio: "0.5", target_share: "0.5" },
      ],
    });
    const carried = mixFlowReducer(refused, { type: "draft_rebased", mix: page9 });
    expect(carried.draft?.basedOn).toBe(9);
    expect(carried.draft?.rows.map((r) => [r.row.sourceId, r.percent])).toEqual([
      [IG, "70"],
      [NEW, "50"],
    ]);
    expect(carried.conflictRevision).toBeNull();
    expect(draftIsStale(carried, page9)).toBe(false);
  });

  it("is dropped only by choice", () => {
    expect(mixFlowReducer(refused, { type: "draft_discarded" }).draft).toBeNull();
  });

  it("saves, clears and keeps the answer as the current mix", () => {
    const done = run(
      [{ type: "draft_save_started" }, { type: "save_answered", action: save, result: ok(mix(9, { mode: "custom" })) }],
      typed,
    );
    expect(done.draft).toBeNull();
    expect(done.local?.revision).toBe(9);
    expect(done.announcement).toBe(
      "Posting mix saved: custom percentages. They apply from the next posting slot.",
    );
  });

  it("keeps the draft after a refusal that is not a conflict, and says why", () => {
    const flow = run(
      [
        { type: "draft_save_started" },
        {
          type: "save_answered",
          action: save,
          result: { ok: false, error: "mix_membership_change_requires_action", status: 400, currentRevision: null },
        },
      ],
      typed,
    );
    expect(flow.draft).toBe(typed.draft);
    expect(flow.error).toBe(SOURCE_REFUSAL_COPY.mix_membership_change_requires_action);
  });
});

describe("one way of changing the mix at a time", () => {
  it("opens no draft while a change is open, and previews no change while a draft is", () => {
    const action = resetEqualAction(8);
    const changing = run([{ type: "change_requested", action }]);
    expect(mixFlowReducer(changing, { type: "draft_opened", mix: mix(8) })).toBe(changing);
    const drafting = run([{ type: "draft_opened", mix: mix(8) }]);
    expect(canRequestChange(drafting)).toBe(false);
    expect(mixFlowReducer(drafting, { type: "change_requested", action })).toBe(drafting);
  });

  it("does not edit a draft while it saves", () => {
    const saving = run([{ type: "draft_opened", mix: mix(8) }, { type: "draft_save_started" }]);
    expect(mixFlowReducer(saving, { type: "draft_edited", sourceId: IG, text: "1" })).toBe(saving);
    expect(editDraft(saving.draft!, IG, "1").rows[0].percent).toBe("1");
  });
});
