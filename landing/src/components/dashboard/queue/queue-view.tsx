"use client";

import { ImageIcon, ListChecks, Loader2, Video } from "lucide-react";
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
import {
  ACTION_LABELS,
  accountLabel,
  formatSlot,
  type Intent,
  type IntentState,
  type QueueAction,
} from "@/lib/intents";
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
 */

/** Labels that differ from the state's own name; the badge falls back to the name. */
const STATE_LABELS: Partial<Record<IntentState, string>> = {
  prompt_pending: "prompting",
  awaiting_approval: "awaiting approval",
  publishing_ambiguous: "needs attention",
  review_required: "needs attention",
};

const STATE_TONE: Partial<Record<IntentState, string>> = {
  awaiting_approval: "bg-amber-100 text-amber-900",
  approved: "bg-blue-100 text-blue-900",
  publishing: "bg-blue-100 text-blue-900",
  publishing_ambiguous: "bg-red-100 text-red-900",
  review_required: "bg-red-100 text-red-900",
};

const ACTION_VARIANT: Record<
  QueueAction,
  "default" | "outline" | "destructive"
> = {
  approve: "default",
  mark_posted: "default",
  skip: "outline",
  reject: "destructive",
  retry: "default",
  resolve_posted: "outline",
  resolve_cancel: "destructive",
};

/** The actions that ask first, and what the dialog says. */
const CONFIRM: Partial<
  Record<QueueAction, { title: string; body: (intent: Intent) => string; verb: string }>
> = {
  reject: {
    title: "Reject this post?",
    body: (intent) =>
      `${intent.file_name} will never be offered again for ${accountLabel(intent)}. Skip instead if it should come back later.`,
    verb: "Reject",
  },
  resolve_cancel: {
    title: "Give up on this post?",
    body: (intent) =>
      `Storydump will stop trying to post ${intent.file_name}. If you can already see it on Instagram, choose It posted instead.`,
    verb: "Give up",
  },
  // The answer to the review's own question. The port needs it when the
  // publish answer was lost: a plain retry could show the story twice.
  retry: {
    title: "Is it on your story?",
    body: (intent) =>
      `Check Instagram first. If ${intent.file_name} is already there, choose It posted — posting again would show it twice. If it is not there, post it again.`,
    verb: "Not there — post again",
  },
};

/**
 * The line under a row. `alert` is a refusal and is announced at once;
 * `status` says what a tap did and waits its turn.
 */
export type RowNote = { text: string; tone: "alert" | "status" };

export function QueueView({
  intents,
  tz,
  truncatedAt,
  pending,
  actionsOf,
  noteFor,
  onAction,
}: {
  intents: Intent[];
  tz: string;
  /** The page limit when the list hit it, so the reader knows it is a page. */
  truncatedAt: number | null;
  /** The row whose action is in flight; every button waits while one is. */
  pending: string | null;
  /** The levers a row offers. */
  actionsOf: (intent: Intent) => QueueAction[];
  /** The line under a row, if it has one. */
  noteFor: (intent: Intent) => RowNote | null;
  onAction: (intent: Intent, action: QueueAction) => void;
}) {
  if (intents.length === 0) {
    return (
      <EmptyState
        icon={ListChecks}
        title="Nothing is waiting."
        description="Posts appear here when their slot arrives."
      />
    );
  }

  return (
    <div className="space-y-3">
      <ul className="divide-y rounded-lg border bg-card">
        {intents.map((intent) => {
          const actions = actionsOf(intent);
          const note = noteFor(intent);
          const MediaGlyph = intent.media_kind === "video" ? Video : ImageIcon;

          return (
            <li key={intent.id} className="p-4">
              <div className="flex flex-wrap items-center gap-4">
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-muted">
                  <MediaGlyph
                    className="h-5 w-5 text-muted-foreground"
                    aria-hidden
                  />
                </div>

                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium">{intent.file_name}</p>
                  <p className="text-sm text-muted-foreground">
                    {accountLabel(intent)} ·{" "}
                    {formatSlot(intent.schedule_slot_at, tz)}
                    {intent.category ? ` · ${intent.category}` : ""}
                  </p>
                </div>

                <Badge
                  variant="secondary"
                  className={
                    intent.published_via === "dry_run"
                      ? "bg-purple-100 text-purple-900"
                      : STATE_TONE[intent.state]
                  }
                >
                  {intent.published_via === "dry_run"
                    ? "dry run"
                    : (STATE_LABELS[intent.state] ?? intent.state)}
                </Badge>
                {intent.cancel_requested && (
                  <Badge
                    variant="secondary"
                    className="bg-amber-100 text-amber-900"
                  >
                    Cancelling
                  </Badge>
                )}

                {actions.length > 0 && (
                  <div className="flex flex-wrap items-center gap-2">
                    {pending === intent.id && (
                      <Loader2
                        className="h-4 w-4 animate-spin text-muted-foreground"
                        aria-hidden
                      />
                    )}
                    {actions.map((action) => {
                      const confirm = CONFIRM[action];
                      return confirm ? (
                        <Dialog key={action}>
                          <DialogTrigger asChild>
                            <Button
                              size="sm"
                              variant={ACTION_VARIANT[action]}
                              disabled={pending !== null}
                            >
                              {ACTION_LABELS[action]}
                            </Button>
                          </DialogTrigger>
                          <DialogContent>
                            <DialogHeader>
                              <DialogTitle>{confirm.title}</DialogTitle>
                              <DialogDescription>
                                {confirm.body(intent)}
                              </DialogDescription>
                            </DialogHeader>
                            <DialogFooter>
                              <DialogClose asChild>
                                <Button variant="outline">Keep it</Button>
                              </DialogClose>
                              <DialogClose asChild>
                                <Button
                                  variant="destructive"
                                  onClick={() => onAction(intent, action)}
                                >
                                  {confirm.verb}
                                </Button>
                              </DialogClose>
                            </DialogFooter>
                          </DialogContent>
                        </Dialog>
                      ) : (
                        <Button
                          key={action}
                          size="sm"
                          variant={ACTION_VARIANT[action]}
                          disabled={pending !== null}
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
