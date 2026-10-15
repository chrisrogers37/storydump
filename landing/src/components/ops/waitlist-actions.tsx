"use client";

import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  admissionLine,
  postAdmission,
  rowAction,
  WAITLIST_COPY,
  type AdmissionLine,
  type WaitlistState,
} from "@/lib/ops-waitlist";
import { cn } from "@/lib/utils";

/**
 * The owner's waitlist actions (#1723): a row's Admit or Send again, and the
 * field that admits any address.
 *
 * Each control holds one in-flight state of its own, and is held while its
 * request is out, so it cannot send twice: two Send agains are two emails.
 * Each posts to this tier's route, refreshes the page once the route has
 * answered, so the table shows where the address stands now, and leaves the
 * answer's line. The API decides every admission (`require_ops`); a refusal
 * is its answer, said.
 */

/** One control's state: whether its request is in flight, and the line its last answer left. */
type ActionStatus = { pending: boolean; line: AdmissionLine | null };

const IDLE: ActionStatus = { pending: false, line: null };

/**
 * One admission, through *setStatus*: in flight, then the answer's line, and
 * a refresh once the route has answered (a request that never left the
 * browser has nothing new to show). Resolves to whether it went as asked: a
 * success whose answer says so. Neither alone is a yes (#1740): a 200 that
 * names no outcome is not, and a refusal is not, however its reason is spelled.
 */
async function submit(
  email: string,
  resend: boolean,
  setStatus: (status: ActionStatus) => void,
  refresh: () => void,
): Promise<boolean> {
  setStatus({ pending: true, line: null });
  const result = await postAdmission(email, resend);
  const line = admissionLine(result);
  setStatus({ pending: false, line });
  if (result.status !== 0) refresh();
  return result.ok && line.ok;
}

function Said({ line, className }: { line: AdmissionLine | null; className?: string }) {
  if (!line) return null;
  return (
    <p
      role="status"
      className={cn("text-xs", line.ok ? "text-muted-foreground" : "text-destructive", className)}
    >
      {line.text}
    </p>
  );
}

/** A row's one action: Admit or Send again, or nothing once the address has signed up. */
export function WaitlistAction({ email, state }: { email: string; state: WaitlistState }) {
  const router = useRouter();
  const [status, setStatus] = useState<ActionStatus>(IDLE);
  const action = rowAction(state);
  if (action === null) return null;
  const resend = action === "resend";
  const [idle, busy] = resend
    ? [WAITLIST_COPY.resend, WAITLIST_COPY.resending]
    : [WAITLIST_COPY.admit, WAITLIST_COPY.admitting];

  return (
    <div className="flex flex-col items-end gap-1">
      <Button
        type="button"
        size="sm"
        variant={resend ? "outline" : "default"}
        disabled={status.pending}
        aria-busy={status.pending}
        onClick={() => submit(email, resend, setStatus, () => router.refresh())}
      >
        {status.pending && <Loader2 className="animate-spin" aria-hidden />}
        {status.pending ? busy : idle}
      </Button>
      <Said line={status.line} className="max-w-56 text-right" />
    </div>
  );
}

/** "Admit an address": one field and one button, for any address, on the waitlist or not. */
export function AdmitAddress() {
  const router = useRouter();
  const [status, setStatus] = useState<ActionStatus>(IDLE);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (status.pending) return;
    const form = event.currentTarget;
    const field = form.elements.namedItem("email") as HTMLInputElement | null;
    const admitted = await submit(field?.value ?? "", false, setStatus, () => router.refresh());
    if (admitted) form.reset();
  }

  return (
    <form onSubmit={onSubmit} aria-labelledby="admit-an-address" className="space-y-2">
      <h2 id="admit-an-address" className="text-sm font-semibold">
        {WAITLIST_COPY.admitHeading}
      </h2>
      <p className="text-sm text-muted-foreground">{WAITLIST_COPY.admitHint}</p>
      <div className="flex gap-2">
        <label htmlFor="admit-email" className="sr-only">
          {WAITLIST_COPY.admitLabel}
        </label>
        {/* Text, not `type="email"`: what an address is stays the API's call. */}
        <Input
          id="admit-email"
          name="email"
          type="text"
          inputMode="email"
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          placeholder="person@example.com"
        />
        <Button type="submit" disabled={status.pending} aria-busy={status.pending}>
          {status.pending && <Loader2 className="animate-spin" aria-hidden />}
          {status.pending ? WAITLIST_COPY.admitting : WAITLIST_COPY.admit}
        </Button>
      </div>
      <Said line={status.line} />
    </form>
  );
}
