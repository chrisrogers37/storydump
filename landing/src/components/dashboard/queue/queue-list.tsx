"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
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
  COMMAND_LABELS,
  accountLabel,
  actionsFor,
  formatSlot,
  refusalCopy,
  type Intent,
  type IntentState,
  type QueueCommand,
} from "@/lib/intents";

/**
 * The queue's rows and their buttons.
 *
 * NO OPTIMISTIC UPDATE, deliberately. The ledger is the authority on what an
 * intent is (`02` §4): a tap is a command, the answer is the row's new state,
 * and the list re-reads after every answer — including a refusal, because a
 * 409 is the ledger saying the row already moved and the honest response is
 * to show where it went. Painting the row as posted before the API agreed is
 * how a double-tapped Telegram card once posted twice.
 *
 * Reject asks first. It is the one action whose lock is permanent — the
 * story is never offered again — and the button sits beside Skip, whose
 * lock expires.
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

const COMMAND_VARIANT: Record<QueueCommand, "default" | "outline" | "destructive"> = {
  approve: "default",
  mark_posted: "default",
  skip: "outline",
  reject: "destructive",
};

type Notice = { intentId: string; text: string };

export function QueueList({
  workspaceId,
  intents,
  tz,
  apiPublishingEnabled,
  truncatedAt,
}: {
  workspaceId: string;
  intents: Intent[];
  tz: string;
  apiPublishingEnabled: boolean;
  /** The page limit when the list hit it, so the reader knows it is a page. */
  truncatedAt: number | null;
}) {
  const router = useRouter();
  const [pending, setPending] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);

  async function run(intent: Intent, command: QueueCommand) {
    setPending(intent.id);
    setNotice(null);

    try {
      const response = await fetch(
        `/api/workspaces/${workspaceId}/commands/${command}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ intent_id: intent.id }),
        },
      );

      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        setNotice({ intentId: intent.id, text: refusalCopy(body?.error) });
        // A refusal about the ROW (already moved on, no longer here) means the
        // list is stale; a refusal about the request or the session does not.
        if (response.status === 409 || response.status === 404) router.refresh();
        return;
      }

      router.refresh();
    } catch {
      setNotice({ intentId: intent.id, text: refusalCopy("target_router_unreachable") });
    } finally {
      setPending(null);
    }
  }

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
          const actions = actionsFor(intent.state, apiPublishingEnabled, intent.cancel_requested);
          const MediaGlyph = intent.media_kind === "video" ? Video : ImageIcon;

          return (
            <li key={intent.id} className="p-4">
              <div className="flex flex-wrap items-center gap-4">
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-muted">
                  <MediaGlyph className="h-5 w-5 text-muted-foreground" aria-hidden />
                </div>

                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium">{intent.file_name}</p>
                  <p className="text-sm text-muted-foreground">
                    {accountLabel(intent)} · {formatSlot(intent.schedule_slot_at, tz)}
                    {intent.category ? ` · ${intent.category}` : ""}
                  </p>
                </div>

                <Badge variant="secondary" className={STATE_TONE[intent.state]}>
                  {STATE_LABELS[intent.state] ?? intent.state}
                </Badge>
                {intent.cancel_requested && (
                  <Badge variant="secondary" className="bg-amber-100 text-amber-900">
                    Cancelling
                  </Badge>
                )}

                {actions.length > 0 && (
                  <div className="flex flex-wrap items-center gap-2">
                    {pending === intent.id && (
                      <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-hidden />
                    )}
                    {actions.map((command) =>
                      command === "reject" ? (
                        <Dialog key={command}>
                          <DialogTrigger asChild>
                            <Button
                              size="sm"
                              variant={COMMAND_VARIANT[command]}
                              disabled={pending !== null}
                            >
                              {COMMAND_LABELS[command]}
                            </Button>
                          </DialogTrigger>
                          <DialogContent>
                            <DialogHeader>
                              <DialogTitle>Reject this post?</DialogTitle>
                              <DialogDescription>
                                {intent.file_name} will never be offered again for{" "}
                                {accountLabel(intent)}. Skip instead if it should come back later.
                              </DialogDescription>
                            </DialogHeader>
                            <DialogFooter>
                              <DialogClose asChild>
                                <Button variant="outline">Keep it</Button>
                              </DialogClose>
                              <DialogClose asChild>
                                <Button
                                  variant="destructive"
                                  onClick={() => void run(intent, "reject")}
                                >
                                  Reject
                                </Button>
                              </DialogClose>
                            </DialogFooter>
                          </DialogContent>
                        </Dialog>
                      ) : (
                        <Button
                          key={command}
                          size="sm"
                          variant={COMMAND_VARIANT[command]}
                          disabled={pending !== null}
                          onClick={() => void run(intent, command)}
                        >
                          {COMMAND_LABELS[command]}
                        </Button>
                      ),
                    )}
                  </div>
                )}
              </div>

              {notice?.intentId === intent.id && (
                <p role="alert" className="mt-2 text-sm text-destructive">
                  {notice.text}
                </p>
              )}
            </li>
          );
        })}
      </ul>

      {truncatedAt !== null && (
        <p className="text-xs text-muted-foreground">
          Showing the first {truncatedAt} posts. More are waiting beyond this page.
        </p>
      )}
    </div>
  );
}
