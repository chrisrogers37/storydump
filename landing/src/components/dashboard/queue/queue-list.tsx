"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { callBff, postJson, type BffResult } from "@/lib/bff";
import {
  commandPath,
  rescheduleRefusalCopy,
  submitRescheduleItem,
  type RescheduleChange,
} from "@/lib/command-client";
import { QueueView } from "@/components/dashboard/queue/queue-view";
import {
  actionsFor,
  refusalCopy,
  requestFor,
  type Intent,
  type IntentKeyedAction,
  type TapAction,
} from "@/lib/intents";

/**
 * The real Queue: `QueueView`'s rows, with the levers the matrix allows and a
 * tap that sends the command.
 *
 * NO OPTIMISTIC UPDATE, deliberately. The ledger is the authority on what an
 * intent is: a tap is a command, the answer is the row's new state,
 * and the list re-reads after every answer — including a refusal, because a
 * 409 is the ledger saying the row already moved and the honest response is
 * to show where it went. Painting the row as posted before the API agreed is
 * how a double tap posts twice.
 */

/**
 * The command one tap sends, and the answer. Exported so the request it puts
 * on the wire is pinned (`queue-list.test.tsx`), not just the button.
 *
 * `callBff` and `commandPath`, NOT `submitCommand` (#1344). The queue's
 * commands are intent-keyed, so they carry no `submission_id`, and a
 * double-clicked Approve REPLAYS — which `submitCommand` reports as a failure
 * and this screen treats as success. Routing this through it unchanged would
 * turn a double tap into an error banner. The path spelling and the wire
 * shape are shared; the replay rule is a real difference, so folding the two
 * is not a cleanup. Reschedule… is the exception that proves it:
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

  async function run(intent: Intent, action: TapAction) {
    setPending(intent.id);
    setNotice(null);

    try {
      // Stop repeating (#485) clears the rule, `reschedule_item` with
      // `repeat: null`, keyed per submission; every other tap is keyed on the
      // intent. A refusal reads the same either way.
      const result =
        action === "stop_repeating"
          ? await submitRescheduleItem(workspaceId, intent.id, { repeat: null })
          : await sendQueueAction(workspaceId, intent, action);

      if (!result.ok) {
        setNotice({ intentId: intent.id, text: refusalCopy(result.error) });
        // A refusal about the ROW (already moved on, no longer here) means the
        // list is stale; a refusal about the request or the session does not.
        // `unreachable` carries status 0, so it correctly refreshes nothing —
        // and `refusalCopy` answers it with the same sentence as
        // `target_router_unreachable`.
        if (result.status === 409 || result.status === 404) router.refresh();
        return;
      }

      router.refresh();
    } finally {
      setPending(null);
    }
  }

  /**
   * Reschedule… moves a planned story, and Edit series… changes its rule too
   * (#485). Its answer goes back to the dialog, which shows a refusal in
   * place; as for a tap, a refusal about the ROW (already moved on, no longer
   * here) also re-reads the list.
   */
  async function reschedule(intent: Intent, change: RescheduleChange): Promise<string | null> {
    setPending(intent.id);
    setNotice(null);

    try {
      const result = await submitRescheduleItem(workspaceId, intent.id, change);
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
      workspaceId={workspaceId}
      tz={tz}
      truncatedAt={truncatedAt}
      pending={pending}
      actionsOf={(intent) => actionsFor(intent, apiPublishingEnabled)}
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
