"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { callBff, postJson, type BffResult } from "@/lib/bff";
import {
  commandPath,
  rescheduleRefusalCopy,
  submitRescheduleItem,
} from "@/lib/command-client";
import { QueueView } from "@/components/dashboard/queue/queue-view";
import {
  actionsFor,
  refusalCopy,
  requestFor,
  type Intent,
  type IntentKeyedAction,
} from "@/lib/intents";

/**
 * The real Queue: `QueueView`'s rows, with the levers the matrix allows and a
 * tap that sends the command.
 *
 * NO OPTIMISTIC UPDATE, deliberately. The ledger is the authority on what an
 * intent is (`02` §4): a tap is a command, the answer is the row's new state,
 * and the list re-reads after every answer — including a refusal, because a
 * 409 is the ledger saying the row already moved and the honest response is
 * to show where it went. Painting the row as posted before the API agreed is
 * how a double-tapped Telegram card once posted twice.
 */

/**
 * The command one tap sends, and the answer. Exported so the request it puts
 * on the wire is pinned (`queue-list.test.tsx`), not just the button.
 *
 * `callBff` and `commandPath`, NOT `submitCommand` (#1344). The queue's
 * commands are intent-keyed, so they carry no `submission_id`, and a
 * double-clicked Approve REPLAYS — which `submitCommand` reports as a failure
 * and this screen has always treated as success. Routing this through it
 * unchanged would turn a double tap into an error banner. Sharing the path
 * spelling and the wire shape is the part that was pure duplication; the
 * replay rule is a real difference. Folding the two is a follow-up with its
 * own decision, not a cleanup. Reschedule… is the exception that proves it:
 * keyed per submission, it goes through `submitCommand` (`reschedule` below).
 */
export function sendQueueAction(
  workspaceId: string,
  intent: Intent,
  action: IntentKeyedAction,
): Promise<BffResult> {
  const { command, body } = requestFor(action, intent);
  return callBff(commandPath(workspaceId, command), postJson(body));
}

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

  async function run(intent: Intent, action: IntentKeyedAction) {
    setPending(intent.id);
    setNotice(null);

    try {
      const result = await sendQueueAction(workspaceId, intent, action);

      if (!result.ok) {
        setNotice({ intentId: intent.id, text: refusalCopy(result.error) });
        // A refusal about the ROW (already moved on, no longer here) means the
        // list is stale; a refusal about the request or the session does not.
        // `unreachable` carries status 0, so it correctly refreshes nothing —
        // and `refusalCopy` answers it with the same sentence the thrown
        // `fetch` used to reach through `target_router_unreachable`.
        if (result.status === 409 || result.status === 404) router.refresh();
        return;
      }

      router.refresh();
    } finally {
      setPending(null);
    }
  }

  /**
   * Reschedule… moves a planned story. Its answer goes back to the dialog,
   * which shows a refusal in place; as for a tap, a refusal about the ROW
   * (already moved on, no longer here) also re-reads the list.
   */
  async function reschedule(intent: Intent, localAt: string): Promise<string | null> {
    setPending(intent.id);
    setNotice(null);

    try {
      const result = await submitRescheduleItem(workspaceId, intent.id, localAt);
      if (result.ok) {
        router.refresh();
        return null;
      }
      if (result.status === 409 || result.status === 404) router.refresh();
      return rescheduleRefusalCopy(result.error, result.status, result.facts);
    } finally {
      setPending(null);
    }
  }

  return (
    <QueueView
      intents={intents}
      tz={tz}
      truncatedAt={truncatedAt}
      pending={pending}
      actionsOf={(intent) =>
        actionsFor(
          intent.state,
          apiPublishingEnabled,
          intent.cancel_requested,
          intent.publish_step,
          intent.origin,
          Boolean(intent.link_url),
        )
      }
      noteFor={(intent) =>
        notice?.intentId === intent.id
          ? { text: notice.text, tone: "alert" }
          : null
      }
      onAction={(intent, action) => void run(intent, action)}
      onReschedule={reschedule}
    />
  );
}
