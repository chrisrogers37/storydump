"use client";

import { useId, useState } from "react";
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
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { MediaRow } from "@/lib/dashboard-payloads";
import { describedBy } from "@/lib/utils";

/**
 * Whether a typed link can be saved over the item's current one: something is
 * typed, and it is not the link already there. A blank field saves nothing;
 * clearing is its own act, Remove link, which sends `null`.
 */
export function canSaveLink(typed: string, current: string | null): boolean {
  const link = typed.trim();
  return link !== "" && link !== (current ?? "");
}

/**
 * Link…: the link an item's stories ask a person to add by hand (#1413 phase 7).
 * A story an app publishes cannot carry a link, so a linked story is posted by
 * hand, and its Queue rows show the link.
 *
 * The port owns the link's rule and refuses a breach by name; the field only
 * says how a link starts. Opens on the current link; Remove link appears only
 * when there is one.
 *
 * It holds the form and nothing else, and reaches no network: `onSubmit` sends
 * the link (or `null` to clear it) and answers with the refusal's sentence, or
 * null once it is saved.
 */
export function LinkDialog({
  item,
  onSubmit,
}: {
  item: MediaRow;
  onSubmit: (item: MediaRow, link: string | null) => Promise<string | null>;
}) {
  const ids = useId();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  function onOpenChange(next: boolean) {
    if (pending) return;
    if (next) {
      setTyped(item.link_url ?? "");
      setNotice(null);
    }
    setOpen(next);
  }

  async function send(link: string | null) {
    if (pending) return;
    // An alert whose text does not change is not read again, so the same
    // refusal twice would pass in silence: clear it before asking.
    setNotice(null);
    setPending(true);
    try {
      const refusal = await onSubmit(item, link);
      if (refusal === null) setOpen(false);
      else setNotice(refusal);
    } finally {
      setPending(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          aria-label={`Link ${item.file_name}`}
        >
          Link…
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="break-words">Link for {item.file_name}</DialogTitle>
          <DialogDescription>
            Instagram doesn&apos;t let an app add a link to a story, so a linked story is
            posted by hand. The link shows on this item&apos;s stories in the Queue.
          </DialogDescription>
        </DialogHeader>
        <form
          className="space-y-4"
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            if (canSaveLink(typed, item.link_url)) void send(typed);
          }}
        >
          <div className="space-y-2">
            <Label htmlFor={`${ids}-link`}>Link</Label>
            <Input
              id={`${ids}-link`}
              type="url"
              inputMode="url"
              autoComplete="url"
              placeholder="https://"
              aria-describedby={describedBy(`${ids}-hint`, notice && `${ids}-notice`)}
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
            />
            <p id={`${ids}-hint`} className="text-xs text-muted-foreground">
              Starts with https://.
            </p>
          </div>
          {notice && (
            <p id={`${ids}-notice`} role="alert" className="text-sm text-destructive">
              {notice}
            </p>
          )}
          <DialogFooter>
            {item.link_url && (
              <Button
                type="button"
                variant="ghost"
                className="sm:mr-auto"
                disabled={pending}
                onClick={() => void send(null)}
              >
                Remove link
              </Button>
            )}
            <DialogClose asChild>
              <Button type="button" variant="outline" disabled={pending}>
                Keep it
              </Button>
            </DialogClose>
            <Button type="submit" disabled={pending || !canSaveLink(typed, item.link_url)}>
              {pending ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : null}
              Save link
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
