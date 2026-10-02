"use client";

import { useId, useState } from "react";
import Link from "next/link";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  NO_PUSH_BINDING,
  scheduleOverrideCopy,
  scheduleRefusalCopy,
  submitScheduleItem,
  type SchedulePlan,
  type SubmitResult,
} from "@/lib/command-client";
import type { MediaRow } from "@/lib/dashboard-payloads";
import { destinationIsActive, destinationName, destinationStateBadge } from "@/lib/destination";
import { formatSlot } from "@/lib/intents";
import type { Destination } from "@/lib/types";

/**
 * Schedule…: plan one library item onto one account at a chosen time (#1413
 * phase 6). At that time the story is sent for approval like any other, and
 * nothing posts until a person approves it.
 *
 * The time is the ACCOUNT's. A person types a date and a time, the dialog
 * names the zone, and the port resolves it there (`schedule_item`). Nothing
 * here converts a time between zones, so the browser's own zone plays no part.
 *
 * What the dialog says, and what a press sends, is decided by the pure
 * functions below, which `schedule-dialog.test.ts` pins; the component holds
 * state and nothing else.
 */

export type ScheduleZone = { zone: string; source: "account" | "workspace" | "default" };

/** The clock a planned time is read on: the port's `COALESCE(a.tz, w.tz)`, then its UTC fallback (`_tz`). */
export function scheduleZone(
  account: Pick<Destination, "tz">,
  workspaceTz: string | null,
): ScheduleZone {
  if (account.tz) return { zone: account.tz, source: "account" };
  if (workspaceTz) return { zone: workspaceTz, source: "workspace" };
  return { zone: "UTC", source: "default" };
}

export function zoneNote({ zone, source }: ScheduleZone): string {
  switch (source) {
    case "account":
      return `In ${zone}, this account's time zone.`;
    case "workspace":
      return `In ${zone}, the workspace's time zone.`;
    case "default":
      return "In UTC: no time zone is set for this account or the workspace.";
  }
}

/** An account as the picker lists it: named as its Accounts row is, with its state when not active. */
export function accountChoiceLabel(
  account: Pick<Destination, "display_name" | "handle" | "state">,
): string {
  const name = destinationName(account);
  return destinationIsActive(account.state)
    ? name
    : `${name} — ${destinationStateBadge(account.state).label}`;
}

export type ScheduledOutcome = { when: string | null; noChat: boolean };

/** What the dialog may say once the port has planned the story: only what its answer says. */
export function scheduledOutcome(data: Record<string, unknown>): ScheduledOutcome {
  const at = data.schedule_slot_at;
  const tz = data.tz;
  return {
    when: typeof at === "string" && typeof tz === "string" ? `${formatSlot(at, tz)} (${tz})` : null,
    noChat: Array.isArray(data.warnings) && data.warnings.includes(NO_PUSH_BINDING),
  };
}

export type ScheduleStep =
  | { kind: "done"; outcome: ScheduledOutcome }
  | { kind: "confirm"; copy: string }
  | { kind: "refused"; copy: string };

/**
 * Where one answer from the port leaves the dialog. The override is offered
 * only on the port's word that it can be (`facts.overridable`), and only once:
 * a refusal of the override itself is final.
 */
export function scheduleStep(result: SubmitResult, overriding: boolean): ScheduleStep {
  if (result.ok) return { kind: "done", outcome: scheduledOutcome(result.data) };
  if (result.error === "locked" && result.facts?.overridable === true && !overriding) {
    return { kind: "confirm", copy: scheduleOverrideCopy(result.facts) };
  }
  return {
    kind: "refused",
    copy: scheduleRefusalCopy(result.error, result.status, result.facts),
  };
}

/**
 * What one press sends. The step it is pressed from decides the override, so
 * no button chooses it: only the question `scheduleStep` opened on the port's
 * word sends `override_locks`, and the form never does.
 */
export function schedulePlan(
  from: "form" | "confirm",
  pick: Omit<SchedulePlan, "overrideLocks">,
): SchedulePlan {
  return from === "confirm" ? { ...pick, overrideLocks: true } : { ...pick };
}

/** An `aria-describedby` from the ids of the sentences on screen, or none. */
function describedBy(...ids: (string | false | null)[]): string | undefined {
  const present = ids.filter((id): id is string => Boolean(id));
  return present.length > 0 ? present.join(" ") : undefined;
}

/** The accounts and the workspace's zone, or null when they could not be read. */
export type ScheduleTargets = { accounts: Destination[]; workspaceTz: string | null } | null;

type Phase =
  | { kind: "form"; notice: string | null }
  | { kind: "confirm"; copy: string }
  | { kind: "done"; outcome: ScheduledOutcome; accountName: string };

export function ScheduleDialog({
  workspaceId,
  item,
  targets,
  onClose,
}: {
  workspaceId: string;
  /** The item being scheduled; the dialog is open while there is one. */
  item: MediaRow | null;
  targets: ScheduleTargets;
  onClose: () => void;
}) {
  const ids = useId();
  const accounts = targets?.accounts ?? [];
  const [accountId, setAccountId] = useState(accounts[0]?.id ?? "");
  const [localAt, setLocalAt] = useState("");
  const [phase, setPhase] = useState<Phase>({ kind: "form", notice: null });
  const [pending, setPending] = useState(false);

  const account = accounts.find((a) => a.id === accountId) ?? null;
  const zone = account && targets ? scheduleZone(account, targets.workspaceTz) : null;

  function close() {
    setLocalAt("");
    setPhase({ kind: "form", notice: null });
    onClose();
  }

  async function submit() {
    if (!item || !account || !localAt || phase.kind === "done") return;
    const plan = schedulePlan(phase.kind, { accountId: account.id, itemId: item.id, localAt });
    setPending(true);
    try {
      const step = scheduleStep(
        await submitScheduleItem(workspaceId, plan),
        plan.overrideLocks === true,
      );
      if (step.kind === "done") {
        setPhase({ kind: "done", outcome: step.outcome, accountName: destinationName(account) });
      } else if (step.kind === "confirm") {
        setPhase({ kind: "confirm", copy: step.copy });
      } else {
        setPhase({ kind: "form", notice: step.copy });
      }
    } finally {
      setPending(false);
    }
  }

  const spinner = pending ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : null;

  function body() {
    if (targets === null) {
      return (
        <>
          <p role="alert" className="text-sm">
            Your accounts could not be loaded just now. Reload the page to schedule a story.
          </p>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="outline">Close</Button>
            </DialogClose>
          </DialogFooter>
        </>
      );
    }

    if (accounts.length === 0) {
      return (
        <>
          <p className="text-sm">
            A story is planned onto an Instagram account, and this workspace has none yet.
          </p>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="outline">Close</Button>
            </DialogClose>
            <Button asChild>
              <Link href="/dashboard/settings?tab=accounts">Open Accounts</Link>
            </Button>
          </DialogFooter>
        </>
      );
    }

    // The result and the override's question replace the form, and the pressed
    // button goes with it. So the button that answers each takes focus,
    // described by its sentence, and a screen reader hears what happened.
    if (phase.kind === "done") {
      return (
        <>
          <p id={`${ids}-result`} className="text-sm">
            {phase.outcome.when
              ? `Planned for ${phase.accountName}. Approval is asked on ${phase.outcome.when}.`
              : `Planned for ${phase.accountName}.`}
          </p>
          {phase.outcome.noChat && (
            <p id={`${ids}-no-chat`} className="text-sm text-muted-foreground">
              No Telegram chat is linked to this workspace yet, so nothing is asked until one is.{" "}
              <Link href="/dashboard/settings?tab=integrations" className="underline">
                Link one in Settings
              </Link>
              .
            </p>
          )}
          <DialogFooter>
            <DialogClose asChild>
              <Button
                autoFocus
                aria-describedby={describedBy(
                  `${ids}-result`,
                  phase.outcome.noChat && `${ids}-no-chat`,
                )}
              >
                Done
              </Button>
            </DialogClose>
          </DialogFooter>
        </>
      );
    }

    if (phase.kind === "confirm") {
      return (
        <>
          <p id={`${ids}-question`} className="text-sm">
            {phase.copy}
          </p>
          <DialogFooter>
            <Button
              variant="outline"
              disabled={pending}
              onClick={() => setPhase({ kind: "form", notice: null })}
            >
              Back
            </Button>
            <Button
              autoFocus
              aria-describedby={`${ids}-question`}
              disabled={pending}
              onClick={() => void submit()}
            >
              {spinner}
              Schedule anyway
            </Button>
          </DialogFooter>
        </>
      );
    }

    return (
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        {accounts.length > 1 ? (
          <div className="space-y-2">
            <Label htmlFor={`${ids}-account`}>Account</Label>
            <Select value={accountId} onValueChange={setAccountId}>
              <SelectTrigger id={`${ids}-account`} className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {accounts.map((choice) => (
                  <SelectItem key={choice.id} value={choice.id}>
                    {accountChoiceLabel(choice)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        ) : (
          <p className="text-sm">On {accountChoiceLabel(accounts[0])}.</p>
        )}
        <div className="space-y-2">
          <Label htmlFor={`${ids}-when`}>When</Label>
          <Input
            id={`${ids}-when`}
            type="datetime-local"
            required
            value={localAt}
            onChange={(event) => setLocalAt(event.target.value)}
          />
          {zone && <p className="text-xs text-muted-foreground">{zoneNote(zone)}</p>}
        </div>
        {phase.notice && (
          <p role="alert" className="text-sm text-destructive">
            {phase.notice}
          </p>
        )}
        <DialogFooter>
          <DialogClose asChild>
            <Button type="button" variant="outline" disabled={pending}>
              Cancel
            </Button>
          </DialogClose>
          <Button type="submit" disabled={pending || !localAt || !account}>
            {spinner}
            Schedule
          </Button>
        </DialogFooter>
      </form>
    );
  }

  return (
    <Dialog
      open={item !== null}
      onOpenChange={(open) => {
        if (!open && !pending) close();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="break-words">
            {phase.kind === "done" ? "Scheduled" : `Schedule ${item?.file_name ?? "this story"}`}
          </DialogTitle>
          <DialogDescription>
            At the time you pick, it is sent for approval like any other story. Nothing posts
            until someone approves it.
          </DialogDescription>
        </DialogHeader>
        {body()}
      </DialogContent>
    </Dialog>
  );
}
