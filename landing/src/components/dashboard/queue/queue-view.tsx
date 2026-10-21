"use client";

import { ListChecks, Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/dashboard/empty-state";
import { ItemLink } from "@/components/dashboard/item-link";
import { mediaTile } from "@/components/dashboard/media/media-tile";
import { RescheduleDialog } from "@/components/dashboard/queue/reschedule-dialog";
import { INTENT_STATE_TONE, TONE_CLASS } from "@/components/dashboard/tone";
import { pastPostsProvenance, provenanceLabel, withPastPostsName } from "@/lib/past-posts-media";
import type { RescheduleChange } from "@/lib/command-client";
import {
  ACTION_LABELS,
  accountLabel,
  approvedBy,
  formatSlot,
  storyAnchor,
  type Intent,
  type IntentState,
  type RowAction,
  type TapAction,
} from "@/lib/intents";
import { describeRepeat } from "@/lib/repeat";
import { cn } from "@/lib/utils";

/**
 * The queue's rows, badges, confirm dialogs and buttons, and nothing else.
 *
 * It holds no state and reaches no network: the caller decides which levers a
 * row gets (`actionsOf`), what line sits under it (`noteFor`) and what a tap
 * does (`onAction`). The real Queue (`QueueList`) passes the matrix and sends
 * the command; the sample workspace (`/demo`) passes its own levers and
 * answers in the browser. One row, two callers, so the sample cannot drift
 * from the screen it shows.
 *
 * Reject asks first. It is the one action whose lock is permanent — the
 * story is never offered again — and the button sits beside Skip, whose
 * lock expires. Give up asks too: it ends a review for good, and the person
 * who can see the story on Instagram should choose It posted instead. Post
 * again asks the review's own question — is it on your story? — because the
 * answer travels with the command as the member's verdict.
 *
 * A planned story (#1413) says who planned it, and while it waits in
 * `scheduled` it can be rescheduled or cancelled. Cancel asks first; it is not
 * permanent, as Reject is, so its button is not red.
 *
 * A story a rule approved (#1477) says which, beside its badge: "Approved by
 * YOLO". It adds no lever; the item's Auto-approve is set in the library.
 *
 * A repeating story (#485) says how it repeats. Edit series… is Reschedule…
 * with its rule; Stop repeating asks first; and its Cancel asks which it
 * means, this occurrence or the series. A question with two answers is still
 * one row of `ASK`: each answer is the action it sends.
 */

/** Labels that differ from the state's own name; the badge falls back to the name. */
const STATE_LABELS: Partial<Record<IntentState, string>> = {
  prompt_pending: "prompting",
  awaiting_approval: "awaiting approval",
  publishing_ambiguous: "needs attention",
  review_required: "needs attention",
};

const ACTION_VARIANT: Record<RowAction, "default" | "outline" | "destructive"> = {
  approve: "default",
  mark_posted: "default",
  skip: "outline",
  reject: "destructive",
  retry: "default",
  resolve_posted: "outline",
  resolve_cancel: "destructive",
  reschedule: "outline",
  cancel: "outline",
  edit_series: "outline",
  stop_repeating: "outline",
};

type Answer = { text: string; variant: "outline" | "destructive"; action: TapAction };
type Question = { title: string; body: string; answers: Answer[] };

/**
 * The buttons that ask first: each one's question, from its row and the row's
 * rule in words (`repeats`, null for none), and the answers that act.
 */
const ASK: Partial<Record<RowAction, (intent: Intent, repeats: string | null) => Question>> = {
  reject: (intent) => ({
    title: "Reject this post?",
    body: `${intent.file_name} will never be offered again for ${accountLabel(intent)}. Skip instead if it should come back later.`,
    answers: [{ text: "Reject", variant: "destructive", action: "reject" }],
  }),
  resolve_cancel: (intent) => ({
    title: "Give up on this post?",
    body: `Storydump will stop trying to post ${intent.file_name}. If you can already see it on Instagram, choose It posted instead.`,
    answers: [{ text: "Give up", variant: "destructive", action: "resolve_cancel" }],
  }),
  // The answer to the review's own question. The port needs it when the
  // publish answer was lost: a plain retry could show the story twice.
  retry: (intent) => ({
    title: "Is it on your story?",
    body: `Check Instagram first. If ${intent.file_name} is already there, choose It posted — posting again would show it twice. If it is not there, post it again.`,
    answers: [{ text: "Not there — post again", variant: "destructive", action: "retry" }],
  }),
  // A repeating story's Cancel asks which it means (#485).
  cancel: (intent, repeats) =>
    repeats
      ? {
          title: "Cancel this one, or the series?",
          body: `${intent.file_name} repeats ${repeats}. Skip this one and the next is still made; stop the series and no later one is.`,
          answers: [
            { text: "Skip this one", variant: "outline", action: "cancel" },
            { text: ACTION_LABELS.cancel_series, variant: "destructive", action: "cancel_series" },
          ],
        }
      : {
          title: "Cancel this planned story?",
          body: `${intent.file_name} won't post to ${accountLabel(intent)}. The item stays in your Media library.`,
          answers: [{ text: "Cancel story", variant: "destructive", action: "cancel" }],
        },
  stop_repeating: (intent, repeats) => ({
    title: "Stop repeating?",
    body: `${intent.file_name} keeps its time, but no later one is made.${repeats ? ` It repeats ${repeats}.` : ""}`,
    answers: [
      { text: ACTION_LABELS.stop_repeating, variant: "destructive", action: "stop_repeating" },
    ],
  }),
};

/**
 * A row's button that asks first: its question, Keep it, and the answers that
 * act. A function returning the elements rather than a component, as
 * `mediaTile` is, so the row's tree holds the dialog itself.
 */
function askFirst(
  action: RowAction,
  disabled: boolean,
  question: Question,
  answer: (action: TapAction) => void,
) {
  return (
    <Dialog key={action}>
      <DialogTrigger asChild>
        <Button size="sm" variant={ACTION_VARIANT[action]} disabled={disabled}>
          {ACTION_LABELS[action]}
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{question.title}</DialogTitle>
          <DialogDescription>{question.body}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">Keep it</Button>
          </DialogClose>
          {question.answers.map((choice) => (
            <DialogClose key={choice.action} asChild>
              <Button variant={choice.variant} onClick={() => answer(choice.action)}>
                {choice.text}
              </Button>
            </DialogClose>
          ))}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * The line under a row. `alert` is a refusal and is announced at once;
 * `status` says what a tap did and waits its turn.
 */
export type RowNote = { text: string; tone: "alert" | "status" };

export function QueueView({
  intents,
  workspaceId,
  tz,
  truncatedAt,
  pending,
  actionsOf,
  noteFor,
  onAction,
  onReschedule,
}: {
  intents: Intent[];
  /**
   * The workspace the rows belong to, for their thumbnails; null where there
   * is no media, as in the sample workspace, and every row draws its glyph.
   */
  workspaceId: string | null;
  tz: string;
  /** The page limit when the list hit it, so the reader knows it is a page. */
  truncatedAt: number | null;
  /** The row whose action is in flight; every button waits while one is. */
  pending: string | null;
  /** The levers a row offers. */
  actionsOf: (intent: Intent) => RowAction[];
  /** The line under a row, if it has one. */
  noteFor: (intent: Intent) => RowNote | null;
  /** A tap, or an answer to a button's question. */
  onAction: (intent: Intent, action: TapAction) => void;
  /**
   * Moves a planned story, or edits its series; answers the refusal's
   * sentence, or null once it landed. A caller without it offers neither
   * Reschedule nor Edit series, whatever `actionsOf` says.
   */
  onReschedule?: (intent: Intent, change: RescheduleChange) => Promise<string | null>;
}) {
  if (intents.length === 0) {
    return (
      <EmptyState
        icon={ListChecks}
        title="Nothing is waiting"
        description="Each Story shows up here when its slot arrives."
      />
    );
  }

  // Every lever waits while one is in flight.
  const disabled = pending !== null;

  return (
    <div className="space-y-3">
      <ul className="divide-y rounded-lg border bg-card">
        {/* A past-posts item goes by its own name; the API sends it, and this is the
            guard for a row that arrives without it. */}
        {intents.map(withPastPostsName).map((intent) => {
          const actions = actionsOf(intent);
          const note = noteFor(intent);
          const byline = approvedBy(intent);
          // The rule's words, once for the row: its badge and its questions.
          const repeats = intent.repeat ? describeRepeat(intent.repeat) : null;
          const origin = pastPostsProvenance(intent);
          return (
            <li
              key={intent.id}
              id={storyAnchor(intent.media_item_id, intent.ig_account_id)}
              className="scroll-mt-6 p-4"
            >
              <div className="flex flex-wrap items-center gap-4">
                {mediaTile({
                  media: intent,
                  workspaceId,
                  box: "h-10 w-10 rounded-md",
                  glyph: "h-5 w-5",
                })}

                {/* Wide enough for the line under the name: on a phone the
                    badge wraps below it rather than squeezing it. */}
                <div className="min-w-0 grow basis-56">
                  <p className="truncate font-medium">{intent.file_name}</p>
                  <p className="text-sm text-muted-foreground">
                    {accountLabel(intent)} ·{" "}
                    {/* One piece: a narrow row breaks the line beside the date, never inside it. */}
                    <span className="whitespace-nowrap">
                      {formatSlot(intent.schedule_slot_at, tz)}
                    </span>
                    {intent.category ? ` · ${intent.category}` : ""}
                  </p>
                  {origin && (
                    <p className="text-xs text-muted-foreground">
                      {provenanceLabel(origin, tz)}
                    </p>
                  )}
                  {intent.link_url && (
                    <ItemLink link={intent.link_url} className="mt-0.5" />
                  )}
                </div>

                <div className="flex flex-wrap items-center gap-2">
                  {intent.origin === "planned" && (
                    <Badge variant="outline">
                      {intent.scheduled_by ? `Planned by ${intent.scheduled_by}` : "Planned"}
                      {repeats &&
                        // The rule's time is the story's own; say whose when it is not the page's.
                        ` · Repeats ${repeats}${intent.tz !== tz ? ` (${intent.tz})` : ""}`}
                    </Badge>
                  )}
                  <Badge
                    variant="secondary"
                    className={
                      TONE_CLASS[
                        intent.published_via === "dry_run"
                          ? "inert"
                          : INTENT_STATE_TONE[intent.state]
                      ]
                    }
                  >
                    {intent.published_via === "dry_run"
                      ? "dry run"
                      : (STATE_LABELS[intent.state] ?? intent.state)}
                  </Badge>
                  {byline && <Badge variant="outline">{byline}</Badge>}
                  {intent.cancel_requested && (
                    <Badge
                      variant="secondary"
                      className={TONE_CLASS.attention}
                    >
                      Cancelling
                    </Badge>
                  )}
                </div>

                {actions.length > 0 && (
                  <div className="flex flex-wrap items-center gap-2">
                    {pending === intent.id && (
                      <Loader2
                        className="h-4 w-4 animate-spin text-muted-foreground"
                        aria-hidden
                      />
                    )}
                    {actions.map((action) => {
                      if (action === "reschedule" || action === "edit_series") {
                        return onReschedule ? (
                          <RescheduleDialog
                            key={action}
                            intent={intent}
                            label={ACTION_LABELS[action]}
                            disabled={disabled}
                            series={action === "edit_series"}
                            onSubmit={onReschedule}
                          />
                        ) : null;
                      }
                      const ask = ASK[action];
                      return ask ? (
                        askFirst(action, disabled, ask(intent, repeats), (answer) =>
                          onAction(intent, answer),
                        )
                      ) : (
                        <Button
                          key={action}
                          size="sm"
                          variant={ACTION_VARIANT[action]}
                          disabled={disabled}
                          onClick={() => onAction(intent, action)}
                        >
                          {ACTION_LABELS[action]}
                        </Button>
                      );
                    })}
                  </div>
                )}
              </div>

              {note && (
                <p
                  role={note.tone}
                  className={cn(
                    "mt-2 text-sm",
                    note.tone === "alert"
                      ? "text-destructive"
                      : "text-muted-foreground",
                  )}
                >
                  {note.text}
                </p>
              )}
            </li>
          );
        })}
      </ul>

      {truncatedAt !== null && (
        <p className="text-xs text-muted-foreground">
          Showing the first {truncatedAt} posts. More are waiting beyond this
          page.
        </p>
      )}
    </div>
  );
}
