"use client";

import { useOptimistic, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Notice } from "@/components/ui/notice";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  APPROVAL_POSITIONS,
  POSITION_DESCRIPTIONS,
  POSITION_LABELS,
  WORKSPACE_DEFAULT,
  approvalsHeldBy,
  asApprovalPosition,
  folderPositionOf,
  positionSavedNotice,
  yoloQuestion,
  type ApprovalPosition,
  type YoloQuestion,
} from "@/lib/approvals";
import {
  settingsRefusalCopy,
  sourceApprovalRefusalCopy,
  submitSetSourceApproval,
  submitSettingsChange,
} from "@/lib/command-client";
import type { SourceRow } from "@/lib/dashboard-payloads";
import { listedDriveSources, sourceFolderName } from "@/lib/drive";
import { Unavailable } from "./unavailable";

/**
 * Settings › General › Approvals (#1477): who approves a story when
 * it is served. The workspace holds a position, and each connected folder
 * holds Workspace default or a position of its own.
 *
 * ── How a choice is sent ─────────────────────────────────────────────────
 *
 * The workspace's position is chosen with the radios and sent with Save, as a
 * `settings_change` of `approval_mode`. Not on each radio: arrow keys move a
 * radio group's choice, so a radio that saved would save every position it
 * passed on the way, Returning items on the way to YOLO among them.
 *
 * Each folder is its own row (`FolderApprovalRow`), whose select sends at once
 * (`set_source_approval`, `null` for Workspace default): a select changes only
 * when a person picks an option.
 *
 * Every send runs in a transition of its own that holds the command and the
 * re-read after it, and shows what it sent as React's optimistic state until
 * the re-read lands. A refusal needs no undo: when the transition ends, the
 * value falls back to the server's. Each control holds itself, with
 * `aria-disabled` rather than `disabled` (a control that becomes disabled loses
 * focus), and a held select does not open. React ties transitions in flight
 * together, so folders saved at the same time may hold each other until all
 * have landed; a pick is never taken and then dropped unseen. Nothing
 * remounts, so the control just used keeps focus.
 *
 * YOLO ASKS ONCE, wherever it is chosen, because it is the choice that posts
 * without anyone looking: the workspace's YOLO, a folder's own, and a folder
 * set to Workspace default while the workspace is on YOLO. The card holds the
 * one question; Save and the rows ask it through `ask`. It opens from the
 * choice rather than from a trigger, so it returns focus to the control that
 * asked itself (`dialog-focus-contract.test.ts` names this file).
 *
 * ── Held, with the reason said at the controls ──────────────────────────
 *
 * The house rule for this tab: a control is live only when the port accepts
 * the write AND something reads the value. While Instagram API posting is
 * off, nothing reads a position, every story asks, and the controls say so.
 * Positions are an admin's, so a member sees them held too.
 */

/**
 * A choice that posts without a tap, asked once before it is sent: the
 * question, the control that asked (where focus goes back), what yes sends,
 * and what keeping things as they are puts back, if anything.
 */
export type YoloAsk = {
  question: YoloQuestion;
  opener: string;
  confirm: () => void;
  keep?: () => void;
};

/** The question on screen. Closed rather than cleared, so its words stay put while it fades. */
export type Asking = YoloAsk & { open: boolean };

const HELD_ID = "approvals-held";
const SAVE_ID = "approval-position-save";

const positionControlId = (position: ApprovalPosition) => `approval-position-${position}`;
const folderControlId = (sourceId: string) => `approval-folder-${sourceId}`;

export function ApprovalsCard({
  workspaceId,
  position,
  sources,
  apiPublishing,
  editable,
}: {
  workspaceId: string;
  /** The workspace's position as the server holds it; null when it could not be read. */
  position: ApprovalPosition | null;
  /** The workspace's sources; the folders listed are the Drive card's. */
  sources: SourceRow[];
  /** Whether the workspace posts through the Instagram API; null when it could not be read. */
  apiPublishing: boolean | null;
  /** Admin or owner: positions are an admin's. */
  editable: boolean;
}) {
  const router = useRouter();
  const folders = listedDriveSources(sources);
  // The radios' choice, saved or not.
  const [chosen, setChosen] = useState<ApprovalPosition | null>(position);
  // The workspace's position as the server last had it: a re-read that moves
  // it puts the radios on it, in place, so the control just used keeps focus.
  const [seenPosition, setSeenPosition] = useState(position);
  if (position !== seenPosition) {
    setSeenPosition(position);
    setChosen(position);
  }
  // The workspace's position as shown: the server's, or the one Save is sending.
  const [shownPosition, showPosition] = useOptimistic(position);
  // Save's command and the re-read after it, as one transition.
  const [saving, startSave] = useTransition();
  const [error, setError] = useState<string | null>(null);
  // The position Save last saved, for the line that says so.
  const [savedTo, setSavedTo] = useState<ApprovalPosition | null>(null);
  const [asking, setAsking] = useState<Asking | null>(null);

  const held = approvalsHeldBy(apiPublishing, editable);
  const locked = held.length > 0;
  const describedBy = locked ? HELD_ID : undefined;
  const canSave = !locked && !saving && chosen !== null && chosen !== shownPosition;
  // Said while the workspace is on the saved position, as shown or as read, so
  // another admin's change, read later, does not leave it standing.
  const savedNotice =
    savedTo !== null && savedTo === shownPosition ? positionSavedNotice(savedTo) : null;

  function ask(yolo: YoloAsk) {
    setAsking({ ...yolo, open: true });
  }

  function savePosition(next: ApprovalPosition) {
    setError(null);
    setSavedTo(null);
    startSave(async () => {
      showPosition(next);
      const result = await submitSettingsChange(workspaceId, { approval_mode: next });
      if (!result.ok) {
        // The radios keep the choice, and Save stays live: it is shown as
        // unsaved, beside the reason, not as the workspace's position.
        setError(settingsRefusalCopy(result.error, result.status));
        return;
      }
      setSavedTo(next);
      router.refresh();
    });
  }

  function save() {
    if (!canSave || chosen === null) return;
    if (chosen === "auto") {
      ask({
        question: yoloQuestion(null),
        opener: SAVE_ID,
        confirm: () => savePosition("auto"),
        // Kept: the radios go back to the position the workspace is on,
        // rather than leave YOLO chosen and Save live.
        keep: () => setChosen(position),
      });
    } else savePosition(chosen);
  }

  function confirm() {
    // Once: a second click while the question fades sends nothing.
    if (!asking?.open) return;
    setAsking({ ...asking, open: false });
    asking.confirm();
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Approvals</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          Whether a story waits for a tap or is approved as soon as it is due. A
          change applies from the next story; one already waiting keeps waiting.
        </p>
        {locked && (
          <p id={HELD_ID} className="text-sm text-muted-foreground">
            {held.join(" ")}
          </p>
        )}
        {error && <Notice tone="error">{error}</Notice>}

        <fieldset className="space-y-2" disabled={locked} aria-describedby={describedBy}>
          <legend className="mb-2 text-sm font-medium">Workspace</legend>
          {position === null ? (
            <Unavailable />
          ) : (
            <>
              {APPROVAL_POSITIONS.map((p) => (
                // The whole box is the click target; the name and the
                // description are read apart, so the name stays the position's.
                <label
                  key={p}
                  htmlFor={positionControlId(p)}
                  className="flex cursor-pointer items-start gap-3 rounded-md border p-3 has-[:checked]:border-primary has-[:disabled]:cursor-not-allowed has-[:disabled]:opacity-60"
                >
                  <input
                    id={positionControlId(p)}
                    type="radio"
                    name="approval-position"
                    value={p}
                    checked={chosen === p}
                    onChange={() => {
                      setSavedTo(null);
                      setChosen(p);
                    }}
                    aria-labelledby={`${positionControlId(p)}-name`}
                    aria-describedby={`${positionControlId(p)}-description`}
                    className="mt-1 accent-primary"
                  />
                  <span className="space-y-0.5">
                    <span id={`${positionControlId(p)}-name`} className="block text-sm font-medium">
                      {POSITION_LABELS[p]}
                    </span>
                    <span
                      id={`${positionControlId(p)}-description`}
                      className="block text-sm text-muted-foreground"
                    >
                      {POSITION_DESCRIPTIONS[p]}
                    </span>
                  </span>
                </label>
              ))}
              {savedNotice && <Notice tone="success">{savedNotice}</Notice>}
              <Button
                id={SAVE_ID}
                size="sm"
                // Held with `aria-disabled`, not `disabled`: focus stays on it
                // through the save and the re-read that follows.
                aria-disabled={!canSave}
                onClick={save}
              >
                {saving ? "Saving..." : "Save"}
              </Button>
            </>
          )}
        </fieldset>

        <div className="space-y-2 border-t pt-4">
          <p className="text-sm font-medium">Folders</p>
          <p className="text-sm text-muted-foreground">
            Each folder follows the workspace unless it has a position of its own.
            An item&apos;s own Auto-approve, set in the Media library, approves it
            whatever its folder says, unless it has a link to add.
          </p>
          {folders.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No folder is connected yet. Connect one under Integrations.
            </p>
          ) : (
            <ul className="divide-y">
              {folders.map((source) => (
                <FolderApprovalRow
                  key={source.id}
                  workspaceId={workspaceId}
                  source={source}
                  // On YOLO as read or as a save in flight would leave it:
                  // Workspace default posts without a tap either way, and a
                  // refused save away from YOLO leaves the folder on it.
                  workspaceOnYolo={position === "auto" || shownPosition === "auto"}
                  locked={locked}
                  describedBy={describedBy}
                  onAsk={ask}
                />
              ))}
            </ul>
          )}
        </div>

        <Dialog
          open={asking?.open ?? false}
          onOpenChange={(open) => {
            if (open || !asking) return;
            setAsking({ ...asking, open: false });
            asking.keep?.();
          }}
        >
          <DialogContent
            onCloseAutoFocus={(event) => {
              const back = asking ? document.getElementById(asking.opener) : null;
              if (!back) return;
              event.preventDefault();
              back.focus();
            }}
          >
            <DialogHeader>
              <DialogTitle className="break-words">{asking?.question.title}</DialogTitle>
              <DialogDescription>{asking?.question.body}</DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <DialogClose asChild>
                <Button variant="outline">Keep it</Button>
              </DialogClose>
              <Button onClick={confirm}>{asking?.question.confirm}</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  );
}

/**
 * One connected folder's position: Workspace default or its own, picked in a
 * select and sent with `set_source_approval` (`null` for Workspace default)
 * through this row's own transition. The row holds itself while its command
 * and the re-read after it are in flight: its select looks held and does not
 * open. Another folder's pick meanwhile is sent and shown, though the two may
 * hold each other until both have landed (React ties their transitions).
 */
export function FolderApprovalRow({
  workspaceId,
  source,
  workspaceOnYolo,
  locked,
  describedBy,
  onAsk,
}: {
  workspaceId: string;
  source: SourceRow;
  /** Whether the workspace is on YOLO, as read or as a save in flight would leave it. */
  workspaceOnYolo: boolean;
  /** Held by the card: API posting off, or a member. */
  locked: boolean;
  /** The card's reason, while it is held. */
  describedBy?: string;
  /** Asks the card's YOLO question before a pick that posts without a tap. */
  onAsk: (yolo: YoloAsk) => void;
}) {
  const router = useRouter();
  // The folder's position as shown: the server's, or the pick being sent.
  const [shown, show] = useOptimistic(folderPositionOf(source.approval_mode));
  // This folder's command and the re-read after it, as one transition.
  const [pending, startTransition] = useTransition();
  const [refusal, setRefusal] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const name = sourceFolderName(source);
  const controlId = folderControlId(source.id);
  const nameAndState = (
    <span className="inline-flex items-center gap-2">
      <span className="truncate">{name}</span>
      {source.state === "paused" && (
        <Badge variant="secondary" className="bg-amber-100 text-amber-900">
          Paused
        </Badge>
      )}
    </span>
  );

  function send(next: ApprovalPosition | null) {
    setRefusal(null);
    startTransition(async () => {
      show(next);
      const result = await submitSetSourceApproval(workspaceId, source.id, next);
      if (!result.ok) {
        // No undo: the shown position falls back to the server's when this ends.
        setRefusal(sourceApprovalRefusalCopy(result.error, result.status));
        return;
      }
      router.refresh();
    });
  }

  function choose(next: ApprovalPosition | null) {
    // Holds itself alone: a pick while its own command or re-read is in flight does nothing.
    if (locked || pending || next === shown) return;
    // Workspace default is YOLO too while the workspace is on YOLO.
    if (next === "auto" || (next === null && workspaceOnYolo)) {
      onAsk({
        question: yoloQuestion(name, next === null),
        opener: controlId,
        confirm: () => send(next),
      });
      return;
    }
    send(next);
  }

  return (
    <li className="flex flex-wrap items-center justify-between gap-3 py-2">
      {shown === undefined ? (
        <>
          <span className="min-w-0 text-sm font-medium">{nameAndState}</span>
          <Unavailable />
        </>
      ) : (
        <>
          <Label htmlFor={controlId} className="min-w-0">
            {nameAndState}
          </Label>
          <Select
            open={open}
            onOpenChange={(next) => {
              // Held: it does not open while its own command or re-read is in
              // flight, so it never takes a pick it would drop.
              if (next && pending) return;
              setOpen(next);
            }}
            value={shown ?? WORKSPACE_DEFAULT}
            onValueChange={(choice) => choose(asApprovalPosition(choice))}
            disabled={locked}
          >
            <SelectTrigger
              id={controlId}
              className="w-48"
              aria-describedby={describedBy}
              // Held with `aria-disabled`, not `disabled`, so it keeps focus.
              aria-disabled={pending}
              // Nor does a closed trigger's typeahead pick while held: Radix
              // skips its own key handler once this one prevents the default.
              onKeyDown={(event) => {
                if (pending && event.key.length === 1) event.preventDefault();
              }}
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={WORKSPACE_DEFAULT}>Workspace default</SelectItem>
              {APPROVAL_POSITIONS.map((p) => (
                <SelectItem key={p} value={p}>
                  {POSITION_LABELS[p]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </>
      )}
      {refusal && (
        <p role="alert" className="w-full text-sm text-destructive">
          {refusal}
        </p>
      )}
    </li>
  );
}
