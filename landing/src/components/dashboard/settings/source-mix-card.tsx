"use client";

import { useId, useReducer, useState } from "react";
import { useRouter } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Notice } from "@/components/ui/notice";
import { Switch } from "@/components/ui/switch";
import { TONE_CLASS } from "@/components/dashboard/tone";
import { indexedBackLine, sourceStatusBadges } from "@/lib/readiness";
import { describedBy } from "@/lib/utils";
import {
  LONG_RUN_COPY,
  MEDIA_CHECKED_COPY,
  MEMBERSHIP_RESET_COPY,
  MODE_COPY,
  checkDraft,
  compareDraft,
  conversionNotice,
  countsLine,
  destinationLabel,
  destinationsDiffer,
  effectivePercents,
  isInstagram,
  offCopy,
  percentLabel,
  percentText,
  previewSourceMix,
  putSourceMix,
  resetEqualAction,
  saveCustomAction,
  setEnabledAction,
  sharePercents,
  sourceAccessibleName,
  sourceSubtitle,
  sourceTitle,
  targetLabel,
  viewedSource,
  type ChangeAction,
  type DraftCheck,
  type DraftRowChange,
  type MixDestination,
  type SourceMix,
  type SourceMixRow,
} from "@/lib/source-mix";
import {
  INITIAL_FLOW,
  awaitingReload,
  canRequestChange,
  changeIsStale,
  currentMix,
  draftIsStale,
  isMixConflict,
  mixFlowReducer,
  type MixFlow,
} from "@/lib/source-mix-flow";
import { SourcePolicyEditor, WRAPPING_BUTTON } from "./source-policy-editor";

/**
 * The Posting mix of a converted workspace (protocol 2): every source, Drive
 * folders and Your Instagram alike, with its share, its counts and an On/Off
 * switch; Equal shares or Custom percentages; Reset to equal. The protocol-1
 * card (`category-weights-card.tsx`) stays for every workspace that has not
 * converted, and `settings/page.tsx` picks between them by the read's
 * `protocol_version`.
 *
 * NOT KEYED ON THE SERVER'S ROWS, unlike the protocol-1 card, and on purpose.
 * That card remounts on every new read, which drops an edit in progress: the
 * right outcome there, where a refresh follows only a save. Here a refresh
 * also follows a CONFLICT, and the draft must survive it. So the page renders
 * this card without a key, `router.refresh()` hands it the new read as props,
 * and the card keeps its own state: the draft against the revision it was
 * opened at, beside whichever reading is newer (`currentMix`). A stale draft
 * is never saved; the person carries it over or drops it.
 *
 * Every decision is in `lib/source-mix-flow.ts` (states) and
 * `lib/source-mix.ts` (shares, labels, the draft); this file wires them to
 * the two doors and draws `SourceMixView`, which takes no hooks so a test can
 * render each state.
 */
export function SourceMixCard({
  workspaceId,
  data,
  editable,
}: {
  workspaceId: string;
  /** The page's read, parsed; null when it could not be read. */
  data: SourceMix | null;
  /** An admin's card. A member's shows the same values, read-only. */
  editable: boolean;
}) {
  const router = useRouter();
  const [flow, dispatch] = useReducer(mixFlowReducer, INITIAL_FLOW);
  const [viewAs, setViewAs] = useState<string | null>(null);
  const ids = useId();
  const current = currentMix(data, flow);

  async function requestChange(action: ChangeAction) {
    if (!canRequestChange(flow)) return;
    dispatch({ type: "change_requested", action });
    const result = await previewSourceMix(workspaceId, action);
    dispatch({ type: "preview_answered", action, result });
    if (isMixConflict(result)) router.refresh();
  }

  async function confirmChange() {
    const pending = flow.pending;
    if (!pending || pending.phase !== "ready" || changeIsStale(flow, current)) return;
    dispatch({ type: "change_confirmed" });
    const result = await putSourceMix(workspaceId, pending.action);
    dispatch({ type: "save_answered", action: pending.action, result });
    // After a save the rest of the page reads the new mix too; after a
    // conflict this card needs the current one.
    if (result.ok || isMixConflict(result)) router.refresh();
  }

  function previewAgain() {
    const pending = flow.pending;
    if (!pending || !current || awaitingReload(flow, current)) return;
    const was = pending.action;
    if (was.action === "reset_equal") {
      void requestChange(resetEqualAction(current.revision));
    } else if (current.rows.some((row) => row.sourceId === was.source_id)) {
      void requestChange(setEnabledAction(current.revision, was.source_id, was.enabled));
    }
  }

  async function saveDraft() {
    const draft = flow.draft;
    if (!draft || flow.savingDraft || draftIsStale(flow, current) || awaitingReload(flow, current)) {
      return;
    }
    const check = checkDraft(draft);
    if (!check.ok) return;
    const action = saveCustomAction(draft.basedOn, check.rows);
    dispatch({ type: "draft_save_started" });
    const result = await putSourceMix(workspaceId, action);
    dispatch({ type: "save_answered", action, result });
    if (result.ok || isMixConflict(result)) router.refresh();
  }

  return (
    <SourceMixView
      current={current}
      flow={flow}
      editable={editable}
      viewAs={viewAs}
      ids={ids}
      workspaceId={workspaceId}
      on={{
        toggle: (row, enabled) => {
          if (current) void requestChange(setEnabledAction(current.revision, row.sourceId, enabled));
        },
        reset: () => {
          if (current) void requestChange(resetEqualAction(current.revision));
        },
        confirm: () => void confirmChange(),
        cancel: () => dispatch({ type: "change_cancelled" }),
        previewAgain,
        openDraft: () => {
          if (current) dispatch({ type: "draft_opened", mix: current });
        },
        editDraft: (sourceId, text) => dispatch({ type: "draft_edited", sourceId, text }),
        saveDraft: () => void saveDraft(),
        discardDraft: () => dispatch({ type: "draft_discarded" }),
        rebaseDraft: () => {
          if (current) dispatch({ type: "draft_rebased", mix: current });
        },
        viewAs: setViewAs,
      }}
    />
  );
}

export type SourceMixHandlers = {
  toggle: (row: SourceMixRow, enabled: boolean) => void;
  reset: () => void;
  confirm: () => void;
  cancel: () => void;
  previewAgain: () => void;
  openDraft: () => void;
  editDraft: (sourceId: string, text: string) => void;
  saveDraft: () => void;
  discardDraft: () => void;
  rebaseDraft: () => void;
  viewAs: (accountId: string) => void;
};

/** The words for a draft that will not save, or null when it will. */
export function draftCheckCopy(check: DraftCheck, rows: readonly SourceMixRow[]): string | null {
  if (check.ok) return null;
  if (check.error === "sum_not_100") {
    return `The percentages add up to ${percentText(check.totalBp)}%, not 100%.`;
  }
  const row = rows.find((r) => r.sourceId === check.sourceId);
  const name = row ? sourceAccessibleName(row) : "a source";
  return check.error === "zero_enabled"
    ? `${name} is on, so it needs a share above 0%. To stop using it, switch it off instead.`
    : `Type a percentage for ${name}: a number up to 100, with at most two decimals.`;
}

const CHANGE_WORDS: Record<DraftRowChange, string> = {
  kept: "unchanged",
  added: "new since you started",
  removed: "removed since you started",
  turned_on: "turned on since you started",
  turned_off: "turned off since you started",
};

/** The card, from its state alone: no hooks, so each state can be rendered in a test. */
export function SourceMixView({
  current,
  flow,
  editable,
  viewAs,
  ids,
  workspaceId,
  on,
}: {
  current: SourceMix | null;
  flow: MixFlow;
  editable: boolean;
  viewAs: string | null;
  ids: string;
  workspaceId: string;
  on: SourceMixHandlers;
}) {
  const conversion = current ? conversionNotice(current.conversionKind, current.mode) : null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Posting mix</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          Each source that is on gets a share of the posts the mix picks. Off keeps a source
          connected without picking from it. Matching posts are the ones whose details fit a
          source&apos;s settings. {MEDIA_CHECKED_COPY} {LONG_RUN_COPY}
        </p>
        {conversion && <Notice tone="info">{conversion}</Notice>}
        {flow.error && <Notice tone="error">{flow.error}</Notice>}
        {current === null ? (
          <p className="text-sm text-muted-foreground">
            The mix could not be loaded just now. Reload to try again.
          </p>
        ) : (
          <MixBody
            mix={current}
            flow={flow}
            editable={editable}
            viewAs={viewAs}
            ids={ids}
            workspaceId={workspaceId}
            on={on}
          />
        )}
      </CardContent>
      {/* One polite region, always present, so each outcome is announced. It is
          the card's last child and a flex item of the CARD, not of its content:
          quiet, it is `sr-only` (absolute, so it takes no room and no gap), and
          said on screen it opens BELOW the rows, which it therefore cannot push.
          Above them it moved the switch just pressed down by about 33px. */}
      <p
        role="status"
        aria-live="polite"
        className={flow.announcement ? "break-words px-6 text-sm text-muted-foreground" : "sr-only"}
      >
        {flow.announcement}
      </p>
    </Card>
  );
}

function MixBody({
  mix,
  flow,
  editable,
  viewAs,
  ids,
  workspaceId,
  on,
}: {
  mix: SourceMix;
  flow: MixFlow;
  editable: boolean;
  viewAs: string | null;
  ids: string;
  workspaceId: string;
  on: SourceMixHandlers;
}) {
  const rows = mix.rows;
  const percents = sharePercents(rows.map((row) => row.targetShare));
  const enabledCount = rows.filter((row) => row.enabled).length;
  const supports = (action: string) => editable && mix.supportedActions.includes(action);
  // The accounts are told apart only where their mixes differ (`destinationsDiffer`):
  // then the selector, the Workspace targets labels and each account's read-only
  // shares are drawn. Several accounts that draw on the same sources, in the same
  // shares, have one mix, so none is drawn as the one being viewed, and no
  // account's matching-post count stands in for the workspace's.
  const accountsDiffer = destinationsDiffer(mix.destinations);
  const viewed: MixDestination | null = accountsDiffer
    ? (mix.destinations.find((d) => d.accountId === viewAs) ?? mix.destinations[0] ?? null)
    : mix.destinations.length === 1
      ? mix.destinations[0]
      : null;
  const effective = accountsDiffer && viewed ? effectivePercents(rows, viewed) : null;
  const pending = flow.pending;
  const staleDraft = draftIsStale(flow, mix);
  const waiting = awaitingReload(flow, mix);
  const draftOpen = flow.draft !== null && !staleDraft && !waiting;
  const check = draftOpen && flow.draft ? checkDraft(flow.draft) : null;
  const invalidRow = check && !check.ok && check.error !== "sum_not_100" ? check.sourceId : null;
  const busy = pending !== null || flow.draft !== null;

  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <div>
          <p className="text-sm font-medium">{MODE_COPY[mix.mode].label}</p>
          <p className="text-sm text-muted-foreground">{MODE_COPY[mix.mode].detail}</p>
        </div>
        {editable && (supports("save_custom") || supports("reset_equal")) && (
          <div className="flex flex-wrap gap-2">
            {supports("save_custom") && enabledCount > 0 && !flow.draft && (
              <Button type="button" variant="outline" size="sm" onClick={on.openDraft} disabled={busy}>
                Edit percentages
              </Button>
            )}
            {supports("reset_equal") && mix.mode === "custom" && (
              <Button type="button" variant="outline" size="sm" onClick={on.reset} disabled={busy}>
                Reset to equal
              </Button>
            )}
          </div>
        )}
        {pending?.action.action === "reset_equal" && (
          <ChangePanel mix={mix} flow={flow} ids={ids} on={on} />
        )}
      </div>

      {rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No sources are connected yet. Each source you connect appears here with its share.
        </p>
      ) : (
        <>
          {enabledCount === 0 && (
            <Notice tone="info">
              <span className="font-medium">No active sources.</span> Nothing is picked for
              posting while every source is off. Turn one on below whenever you are ready.
            </Notice>
          )}
          {flow.draft && (waiting || staleDraft) && (
            <DraftReview mix={mix} flow={flow} waiting={waiting} ids={ids} on={on} />
          )}
          {accountsDiffer && viewed && (
            <div className="space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <label htmlFor={`${ids}-view-as`} className="text-sm font-medium">
                  View as
                </label>
                <select
                  id={`${ids}-view-as`}
                  className="h-9 w-full max-w-xs rounded-md border bg-background px-2 text-sm sm:w-auto"
                  value={viewed.accountId}
                  onChange={(e) => on.viewAs(e.target.value)}
                >
                  {mix.destinations.map((d) => (
                    <option key={d.accountId} value={d.accountId}>
                      {destinationLabel(d)}
                    </option>
                  ))}
                </select>
              </div>
              <p className="break-words text-xs text-muted-foreground">
                Workspace targets are the shares set here. {destinationLabel(viewed)}&apos;s shares
                and counts are read-only: the targets spread over the sources that can serve it.
              </p>
            </div>
          )}
          <p id={`${ids}-rows`} className={accountsDiffer ? "text-sm font-medium" : "sr-only"}>
            {accountsDiffer ? "Workspace targets" : "Sources"}
          </p>
          <ul aria-labelledby={`${ids}-rows`} className="divide-y">
            {rows.map((row, i) => (
              <SourceRowItem
                key={row.sourceId}
                row={row}
                percent={percents[i]}
                effective={effective ? effective[i] : null}
                viewed={viewed}
                accountsDiffer={accountsDiffer}
                mix={mix}
                flow={flow}
                editable={editable && supports("set_enabled")}
                draftOpen={draftOpen}
                invalid={invalidRow === row.sourceId}
                ids={ids}
                workspaceId={workspaceId}
                policyEditable={editable}
                on={on}
              />
            ))}
          </ul>
          {draftOpen && check && (
            <DraftFooter rows={rows} check={check} saving={flow.savingDraft} ids={ids} on={on} />
          )}
        </>
      )}
    </div>
  );
}

function SourceRowItem({
  row,
  percent,
  effective,
  viewed,
  accountsDiffer,
  mix,
  flow,
  editable,
  draftOpen,
  invalid,
  ids,
  workspaceId,
  policyEditable,
  on,
}: {
  row: SourceMixRow;
  percent: number;
  effective: number | null;
  viewed: MixDestination | null;
  accountsDiffer: boolean;
  mix: SourceMix;
  flow: MixFlow;
  editable: boolean;
  draftOpen: boolean;
  invalid: boolean;
  ids: string;
  workspaceId: string;
  policyEditable: boolean;
  on: SourceMixHandlers;
}) {
  const name = sourceAccessibleName(row);
  const counts = countsLine(row, viewed);
  // An archive's own progress as the viewed account's entry for it sent it: a
  // Drive folder has none, and with no account to view there is nothing to say.
  const seen = isInstagram(row) ? viewedSource(row, viewed) : null;
  const badges = sourceStatusBadges(seen);
  const indexedBack = indexedBackLine(seen);
  const offId = `${ids}-${row.sourceId}-off`;
  const blockedId = `${ids}-${row.sourceId}-blocked`;
  const pending = flow.pending;
  const pendingHere =
    pending?.action.action === "set_enabled" && pending.action.source_id === row.sourceId
      ? pending.action
      : null;
  // Past posts that cannot be used here (`archive_available` false) hold the
  // switch only for turning them ON. An archive already On, which is what a
  // rollback leaves behind, can still be switched Off: the API allows it without
  // the capability, because that closes something.
  const archiveUnavailable = isInstagram(row) && !mix.archiveAvailable;
  const archiveBlocked = archiveUnavailable && !row.enabled;
  const switchable = editable && !flow.draft && !pending && !archiveBlocked;
  const draftRow = draftOpen ? flow.draft?.rows.find((r) => r.row.sourceId === row.sourceId) : undefined;

  return (
    <li className="space-y-2 py-3">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0 flex-1 basis-48">
          <p className="break-words text-sm font-medium">{sourceTitle(row)}</p>
          <p className="break-words text-xs text-muted-foreground">{sourceSubtitle(row)}</p>
          {counts && <p className="break-words text-xs text-muted-foreground">{counts}</p>}
          {isInstagram(row) && <p className="text-xs text-muted-foreground">Each carousel slide is its own item.</p>}
          {badges.length > 0 && (
            <div className="flex flex-wrap items-center gap-1 pt-1">
              {badges.map((badge) => (
                <Badge key={badge.label} variant="secondary" className={TONE_CLASS[badge.tone]}>
                  {badge.label}
                </Badge>
              ))}
            </div>
          )}
          {indexedBack && (
            <p className="break-words text-xs text-muted-foreground">{indexedBack}</p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
          <span>
            <span className="text-muted-foreground">
              {draftRow ? "Saved " : accountsDiffer ? "Workspace target " : ""}
            </span>
            <span className="font-medium tabular-nums">
              {targetLabel(row, percent)}
            </span>
          </span>
          {effective !== null && viewed && (
            <span>
              {/* `anywhere`, not `break-words`: a handle is one unbreakable word, and
                  only `anywhere` lowers the line's minimum width. Under `break-words`,
                  a 30-character handle runs past the card at 320px. */}
              <span className="text-muted-foreground [overflow-wrap:anywhere]">{destinationLabel(viewed)} </span>
              <span className="tabular-nums">{percentLabel(effective)}</span>
            </span>
          )}
          {draftRow && row.enabled && (
            <span className="flex items-center gap-1">
              <Input
                inputMode="decimal"
                value={draftRow.percent}
                onChange={(e) => on.editDraft(row.sourceId, e.target.value)}
                disabled={flow.savingDraft}
                aria-label={`${name} percentage`}
                aria-invalid={invalid || undefined}
                aria-describedby={invalid ? `${ids}-draft-check` : undefined}
                className="w-20"
              />
              <span aria-hidden="true">%</span>
            </span>
          )}
          {editable && (
            <Switch
              checked={pendingHere ? pendingHere.enabled : row.enabled}
              disabled={!switchable}
              onCheckedChange={(next) => on.toggle(row, next)}
              aria-label={name}
              aria-describedby={describedBy(offId, archiveUnavailable && blockedId)}
            />
          )}
        </div>
      </div>
      <p id={offId} className="text-xs text-muted-foreground">
        Off: {offCopy(row.provider)}
      </p>
      {archiveUnavailable && editable && (
        <p id={blockedId} className="text-xs text-muted-foreground">
          {row.enabled
            ? "Past posts are paused for this workspace. You can switch this source off, but not back on yet."
            : "Using past posts is not switched on for this workspace yet."}
        </p>
      )}
      {pendingHere && <ChangePanel mix={mix} flow={flow} ids={ids} on={on} />}
      {isInstagram(row) && mix.archiveAvailable && (
        <SourcePolicyEditor
          workspaceId={workspaceId}
          sourceId={row.sourceId}
          sourceName={name}
          editable={policyEditable}
        />
      )}
    </li>
  );
}

/** Preview, then Confirm or Cancel: a change that resets every share to equal. */
function ChangePanel({
  mix,
  flow,
  ids,
  on,
}: {
  mix: SourceMix;
  flow: MixFlow;
  ids: string;
  on: SourceMixHandlers;
}) {
  const pending = flow.pending;
  if (!pending) return null;
  const { action } = pending;
  const row = action.action === "set_enabled" ? mix.rows.find((r) => r.sourceId === action.source_id) : null;
  const gone = action.action === "set_enabled" && !row;
  const stale = changeIsStale(flow, mix);
  const waiting = awaitingReload(flow, mix);
  const title =
    action.action === "reset_equal"
      ? "Reset every share to equal?"
      : `Turn ${action.enabled ? "on" : "off"} ${row ? sourceAccessibleName(row) : "this source"}?`;
  const preview = pending.preview;

  return (
    <div role="group" aria-labelledby={`${ids}-change`} className="space-y-2 rounded-md border p-3">
      <p id={`${ids}-change`} className="break-words text-sm font-medium">
        {title}
      </p>
      <p className="text-sm text-muted-foreground">
        {action.action === "set_enabled"
          ? MEMBERSHIP_RESET_COPY
          : "Every source that is on gets the same share; sources that are off stay off."}
        {mix.mode === "custom" ? " Your custom percentages will be replaced." : ""}
      </p>
      {gone ? (
        <p className="text-sm">This source is no longer in the mix.</p>
      ) : stale ? (
        <p className="text-sm">
          {waiting
            ? "The posting mix changed. Loading the current mix…"
            : "The posting mix changed since this preview. Preview again to see what the change makes now."}
        </p>
      ) : pending.phase === "previewing" || !preview ? (
        <p className="text-sm text-muted-foreground">Working out the new shares…</p>
      ) : (
        <PreviewShares preview={preview} />
      )}
      <div className="flex flex-wrap gap-2">
        {!stale && !gone && (
          <Button
            type="button"
            size="sm"
            onClick={on.confirm}
            disabled={pending.phase !== "ready" || !preview}
          >
            {pending.phase === "saving" ? "Saving..." : "Confirm"}
          </Button>
        )}
        {stale && !gone && (
          <Button type="button" size="sm" onClick={on.previewAgain} disabled={waiting}>
            Preview again
          </Button>
        )}
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={on.cancel}
          disabled={pending.phase === "saving"}
        >
          Cancel
        </Button>
      </div>
    </div>
  );
}

/** The would-be workspace targets and, with several accounts, what each would draw. */
function PreviewShares({ preview }: { preview: SourceMix }) {
  const percents = sharePercents(preview.rows.map((row) => row.targetShare));
  return (
    <div className="space-y-2 text-sm">
      <ul className="space-y-1">
        {preview.rows.map((row, i) => (
          <li key={row.sourceId} className="break-words">
            {sourceTitle(row)} <span className="text-muted-foreground">({sourceSubtitle(row)})</span>:{" "}
            <span className="font-medium tabular-nums">
              {targetLabel(row, percents[i])}
            </span>
          </li>
        ))}
      </ul>
      {destinationsDiffer(preview.destinations) && (
        <div className="space-y-1">
          <p className="text-muted-foreground">What each account would draw:</p>
          <ul className="space-y-1">
            {preview.destinations.map((d) => {
              const shares = effectivePercents(preview.rows, d);
              const drawn = preview.rows
                .map((row, i) => ({ row, share: shares[i] }))
                .filter(({ share }) => share > 0);
              return (
                <li key={d.accountId} className="break-words">
                  {destinationLabel(d)}:{" "}
                  {drawn.length === 0
                    ? "nothing to draw from"
                    : drawn.map(({ row, share }) => `${sourceTitle(row)} ${percentLabel(share)}`).join(", ")}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}

/** A kept draft beside the mix as it is now: every row marked, nothing dropped unseen. */
function DraftReview({
  mix,
  flow,
  waiting,
  ids,
  on,
}: {
  mix: SourceMix;
  flow: MixFlow;
  waiting: boolean;
  ids: string;
  on: SourceMixHandlers;
}) {
  const draft = flow.draft;
  if (!draft) return null;
  const saved = sharePercents(mix.rows.map((row) => row.targetShare));
  const savedBy = new Map(mix.rows.map((row, i) => [row.sourceId, targetLabel(row, saved[i])]));
  return (
    <div role="group" aria-labelledby={`${ids}-review`} className="space-y-2 rounded-md border p-3">
      <p id={`${ids}-review`} className="text-sm font-medium">
        {waiting
          ? "The posting mix changed. Loading the current mix… Your percentages are kept."
          : "Your unsaved percentages, beside the mix as it is now"}
      </p>
      {!waiting && (
        <>
          <ul className="space-y-1 text-sm">
            {compareDraft(draft, mix).map((entry) => (
              <li key={entry.row.sourceId} className="break-words">
                {sourceAccessibleName(entry.row)}: yours{" "}
                {entry.draftEnabled === null ? "—" : entry.draftEnabled ? `${entry.draftPercent}%` : "Off"}
                {" · "}now {savedBy.get(entry.row.sourceId) ?? "—"} · {CHANGE_WORDS[entry.change]}
              </li>
            ))}
          </ul>
          <p className="text-sm text-muted-foreground">
            Nothing was saved. Carry your percentages over to review and save them, or discard them.
          </p>
        </>
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          size="sm"
          className={WRAPPING_BUTTON}
          onClick={on.rebaseDraft}
          disabled={waiting}
        >
          Use my percentages on the current mix
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          className={WRAPPING_BUTTON}
          onClick={on.discardDraft}
        >
          Discard my percentages
        </Button>
      </div>
    </div>
  );
}

function DraftFooter({
  rows,
  check,
  saving,
  ids,
  on,
}: {
  rows: readonly SourceMixRow[];
  check: DraftCheck;
  saving: boolean;
  ids: string;
  on: SourceMixHandlers;
}) {
  const problem = draftCheckCopy(check, rows);
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <p id={`${ids}-draft-check`} className={`min-w-0 break-words text-sm ${problem ? "text-amber-700" : "text-muted-foreground"}`}>
        {problem ?? `Your percentages total ${percentText(check.ok ? check.totalBp : 0)}% · unsaved`}
      </p>
      <div className="flex flex-wrap gap-2">
        <Button type="button" size="sm" variant="outline" onClick={on.discardDraft} disabled={saving}>
          Cancel
        </Button>
        <Button type="button" size="sm" onClick={on.saveDraft} disabled={saving || !check.ok}>
          {saving ? "Saving..." : "Save percentages"}
        </Button>
      </div>
    </div>
  );
}
