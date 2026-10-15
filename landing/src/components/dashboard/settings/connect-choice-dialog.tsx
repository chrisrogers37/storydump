"use client";

import { useId, type RefObject } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Notice } from "@/components/ui/notice";
import { Switch } from "@/components/ui/switch";
import {
  ADD_EXISTING_COPY,
  HISTORY_INDEXING_COPY,
  PUBLISHING_ONLY_COPY,
  RECONNECT_KEEPS_COPY,
  RESET_TO_EQUAL_COPY,
  isZeroShare,
  resetRowName,
  type ConnectPreview,
} from "@/lib/connect-choice";
import { TURN_ON_PAST_POSTS_COPY } from "@/lib/connection-result";
import { sharePercents, targetLabel } from "@/lib/source-mix";

/**
 * Use my past posts as a content source — asked before an Instagram connect
 * leaves for Instagram. The Accounts tab opens it only where the connect
 * preview says past posts are a choice; everywhere else a connect starts
 * without it (`connect-choice.ts`).
 *
 * Three entries, from the preview and never from which button was pressed:
 * the header's Add and a handle-only row's first connect get the editable
 * switch, default from the server, with the equal shares an On choice would
 * leave; a reconnect shows the saved setting it keeps, read-only.
 *
 * A real modal: Radix traps focus, closes on Escape and labels the dialog by
 * its title and description. It opens from state, after the preview answers,
 * so it has no trigger to return focus to; `returnFocusTo` is the button that
 * asked for it (`dialog-focus-contract.test.ts`).
 */

export type ConnectChoiceState = {
  preview: ConnectPreview;
  useHistory: boolean;
  /** Said first, politely: why the dialog is asking again. */
  notice: string | null;
  /** A refusal of the last Continue, said assertively. */
  error: string | null;
  /** A start is in flight: nothing can be changed or closed. */
  busy: boolean;
};

export type ConnectChoiceHandlers = {
  toggle: (on: boolean) => void;
  confirm: () => void;
  cancel: () => void;
};

function named(preview: ConnectPreview): string | null {
  return preview.handle ? `@${preview.handle}` : null;
}

function title(preview: ConnectPreview): string {
  const handle = named(preview);
  if (preview.entry === "reconnect") return handle ? `Reconnect ${handle}` : "Reconnect Instagram";
  if (preview.entry === "first_connect" && handle) return `Connect ${handle}`;
  return "Connect Instagram";
}

function description(preview: ConnectPreview): string {
  const handle = named(preview);
  if (preview.entry === "reconnect") {
    return `Reconnecting renews Storydump's access to ${handle ?? "this account"}. It changes no source setting.`;
  }
  return `Choose whether ${handle ? `${handle}'s` : "this account's"} past posts become a content source before you continue to Instagram.`;
}

/** The switch's label: whose past posts, by handle where the preview names one. */
export function historySwitchLabel(preview: ConnectPreview): string {
  const handle = named(preview);
  return handle
    ? `Use ${handle}'s past posts as a content source`
    : "Use my past posts as a content source";
}

/** The dialog's body, without hooks, so a test can draw each state. */
export function ConnectChoiceView({
  state,
  ids,
  on,
}: {
  state: ConnectChoiceState;
  ids: string;
  on: ConnectChoiceHandlers;
}) {
  const { preview, useHistory, notice, error, busy } = state;
  const reset = useHistory ? preview.reset : null;
  const percents = reset ? sharePercents(reset.map((row) => row.targetShare)) : [];
  return (
    <>
      <DialogHeader>
        <DialogTitle className="break-words">{title(preview)}</DialogTitle>
        <DialogDescription>{description(preview)}</DialogDescription>
      </DialogHeader>

      {notice && <Notice tone="info">{notice}</Notice>}
      {error && <Notice tone="error">{error}</Notice>}

      {preview.entry === "reconnect" ? (
        <div className="space-y-1 text-sm">
          <p className="font-medium">
            Past posts: {preview.savedEnabled ? "On" : "Off"}
          </p>
          <p className="text-muted-foreground">
            {RECONNECT_KEEPS_COPY}
            {preview.savedEnabled ? "" : ` ${TURN_ON_PAST_POSTS_COPY}`}
          </p>
        </div>
      ) : (
        <div className="min-w-0 space-y-3 text-sm">
          <div className="flex items-start justify-between gap-3">
            <Label htmlFor={`${ids}-history`} className="break-words leading-snug">
              {historySwitchLabel(preview)}
            </Label>
            <Switch
              id={`${ids}-history`}
              checked={useHistory}
              onCheckedChange={on.toggle}
              disabled={busy}
              aria-describedby={`${ids}-history-detail`}
            />
          </div>
          <p id={`${ids}-history-detail`} className="text-muted-foreground">
            {HISTORY_INDEXING_COPY}
          </p>
          {preview.entry === "add" && <p className="text-muted-foreground">{ADD_EXISTING_COPY}</p>}
          {!useHistory && <p className="text-muted-foreground">{PUBLISHING_ONLY_COPY}</p>}
          {reset && (
            <div className="space-y-2 rounded-md border p-3">
              <p id={`${ids}-reset`} className="font-medium">
                {RESET_TO_EQUAL_COPY}
              </p>
              <ul aria-labelledby={`${ids}-reset`} className="space-y-1">
                {reset.map((row, i) => (
                  <li
                    key={row.sourceId ?? "new"}
                    className="flex flex-wrap items-baseline justify-between gap-x-3"
                  >
                    <span className="min-w-0 break-words">{resetRowName(row, preview)}</span>
                    <span className="tabular-nums">
                      {targetLabel({ enabled: !isZeroShare(row.targetShare) }, percents[i])}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      <DialogFooter>
        <Button type="button" variant="outline" onClick={on.cancel} disabled={busy}>
          Cancel
        </Button>
        <Button type="button" onClick={on.confirm} disabled={busy}>
          {busy ? "Opening Instagram..." : "Continue to Instagram"}
        </Button>
      </DialogFooter>
    </>
  );
}

export function ConnectChoiceDialog({
  state,
  on,
  returnFocusTo,
}: {
  /** Open while there is one. */
  state: ConnectChoiceState | null;
  on: ConnectChoiceHandlers;
  /** The button that asked for the dialog, where focus goes back on close. */
  returnFocusTo: RefObject<HTMLElement | null>;
}) {
  const ids = useId();
  return (
    <Dialog
      open={state !== null}
      onOpenChange={(open) => {
        // A start in flight is not abandoned by Escape or the ×: it ends in a
        // navigation or a refusal the dialog says.
        if (!open && !state?.busy) on.cancel();
      }}
    >
      <DialogContent
        className="max-h-[calc(100dvh-2rem)] overflow-y-auto"
        onCloseAutoFocus={(event) => {
          const opener = returnFocusTo.current;
          if (!opener) return;
          event.preventDefault();
          opener.focus();
        }}
      >
        {state && <ConnectChoiceView state={state} ids={ids} on={on} />}
      </DialogContent>
    </Dialog>
  );
}
