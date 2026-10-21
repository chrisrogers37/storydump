"use client";

import { useOptimistic, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ItemLink } from "@/components/dashboard/item-link";
import { LinkDialog } from "@/components/dashboard/media/link-dialog";
import { MediaThumbnail } from "@/components/dashboard/media/media-thumbnail";
import { TONE_CLASS } from "@/components/dashboard/tone";
import { RULE_LABELS, autoApproveHint } from "@/lib/approvals";
import {
  PAST_POSTS_MEDIA_CHECKED_COPY,
  pastPostsProvenance,
  provenanceLabel,
  slideLabel,
} from "@/lib/past-posts-media";
import {
  autoApproveRefusalCopy,
  linkRefusalCopy,
  submitSetItemAutoApprove,
  submitSetItemLink,
} from "@/lib/command-client";
import type { MediaRow } from "@/lib/dashboard-payloads";
import { thumbnailFor } from "@/lib/thumbnails";

/**
 * One item in the library: its picture, name, link and badges, and its three
 * actions, Link…, Schedule… and Auto-approve.
 *
 * ── Auto-approve (#1477), and why the card is its own component ──────────
 *
 * The toggle sends `set_item_auto_approve` with the value it moves TO, and
 * shows that value at once, with the 🔁 badge beside the posting badge: React's
 * optimistic state, held by one transition that runs the command and the
 * re-read after it. Once the transition ends the card shows the server's flag
 * again, so a refusal needs no undo (the value falls back by itself, and the
 * refusal is said under the actions), and a flag changed from Telegram's
 * Configure shows as it is. Nothing remounts, so the toggle just pressed keeps
 * focus.
 *
 * The toggle always shows, as Configure does on the card: the flag is a
 * standing Approve, and its tooltip says when it acts. It goes by its rule's
 * name (`RULE_LABELS.item`), the name the Queue's by-line gives a story the
 * flag approved.
 *
 * ── An item from a connected account's past posts (#1644) ─────────────────
 *
 * `item` is the row to draw, a past-posts item already under its own name
 * (`withPastPostsName`). Its picture is asked for in the card's own lane
 * (`PastPostsThumbnail`), and its card says what is known of it and no more:
 * where it came from, and that its media is checked when it is used. It does
 * not say the item matches: the library lists items whatever a source's
 * settings leave out of the pool.
 */

function formatBytes(bytes: number | null): string {
  if (bytes === null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function postingBadge(times: number) {
  if (times === 0)
    return <Badge variant="outline" className="text-xs">Never posted</Badge>;
  if (times === 1)
    return <Badge variant="secondary" className="text-xs">Posted once</Badge>;
  return (
    <Badge variant="secondary" className={`${TONE_CLASS.active} text-xs`}>
      {times}x posted
    </Badge>
  );
}

export function MediaCard({
  item,
  workspaceId,
  tz,
  apiPublishing,
  onSchedule,
}: {
  item: MediaRow;
  workspaceId: string;
  /** The zone the day a past-posts item was posted is read on: the workspace's. */
  tz: string;
  /** Whether the workspace posts through the Instagram API; null when it could not be read. */
  apiPublishing: boolean | null;
  /** Opens the grid's one Schedule dialog for this item; focus goes back to `opener`. */
  onSchedule: (item: MediaRow, opener: HTMLButtonElement) => void;
}) {
  const router = useRouter();
  // The flag as shown: the server's, or the value a toggle is sending.
  // `=== true`: an API that omits the flag reads off, and the toggle keeps aria-pressed.
  const [autoApprove, setAutoApprove] = useOptimistic(item.auto_approve === true);
  // A toggle's command and the re-read after it, as one transition.
  const [pending, startTransition] = useTransition();
  const [notice, setNotice] = useState<string | null>(null);

  // Sets or clears an item's link; answers the refusal's sentence, or null once saved.
  async function saveLink(target: MediaRow, link: string | null): Promise<string | null> {
    const result = await submitSetItemLink(workspaceId, target.id, link);
    if (result.ok) {
      router.refresh();
      return null;
    }
    if (result.status === 404) router.refresh();
    return linkRefusalCopy(result.error, result.status);
  }

  function toggleAutoApprove() {
    // One at a time: a tap while the last, or its re-read, is awaited does nothing.
    if (pending) return;
    const next = !autoApprove;
    setNotice(null);
    startTransition(async () => {
      setAutoApprove(next);
      const result = await submitSetItemAutoApprove(workspaceId, item.id, next);
      if (result.ok) {
        // Inside the transition, so the toggle holds, and shows `next`, until
        // the re-read lands with the server's flag.
        router.refresh();
        return;
      }
      setNotice(autoApproveRefusalCopy(result.error, result.status));
      // A refusal about the item (gone, or removed) means the library is stale.
      if (result.status === 404 || result.status === 409) router.refresh();
    });
  }

  const origin = pastPostsProvenance(item);
  const picture = thumbnailFor(
    {
      file_name: item.file_name,
      media_kind: item.media_kind,
      media_item_id: item.id,
      has_thumbnail: item.has_thumbnail,
      thumbnail_version: item.thumbnail_version,
      provenance: item.provenance,
    },
    workspaceId,
  );

  return (
    <Card className="overflow-hidden">
      <div className="relative flex h-32 items-center justify-center overflow-hidden bg-muted text-muted-foreground">
        {picture ? (
          <MediaThumbnail
            key={picture}
            src={picture}
            alt={item.file_name}
            video={item.media_kind === "video"}
            // The kind label below is drawn under the picture, so a
            // thumbnail that fails leaves it showing.
            fallback={null}
            pastPosts={origin ? { workspaceId, layout: "card" } : undefined}
          />
        ) : null}
        <span className="pointer-events-none text-xs uppercase tracking-wider">
          {item.mime_type?.split("/")[1] || item.media_kind || "file"}
        </span>
      </div>
      <CardContent className="space-y-2 p-3">
        <p className="truncate text-sm font-medium" title={item.file_name}>
          {item.file_name}
        </p>
        {origin?.slide && <p className="text-xs font-medium">{slideLabel(origin.slide)}</p>}
        {origin && (
          <div className="space-y-0.5 text-xs text-muted-foreground">
            <p>{provenanceLabel(origin, tz)}</p>
            <p>{PAST_POSTS_MEDIA_CHECKED_COPY}</p>
          </div>
        )}
        {item.link_url && <ItemLink link={item.link_url} />}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Badge variant="outline" className="text-xs">
            {item.category ?? "uncategorised"}
          </Badge>
          <div className="flex items-center gap-1">
            {autoApprove && (
              <Badge variant="outline" className="text-xs" title={`${RULE_LABELS.item} is on`}>
                <span aria-hidden>🔁</span>
                <span className="sr-only">{`${RULE_LABELS.item} is on`}</span>
              </Badge>
            )}
            {postingBadge(item.times_posted)}
          </div>
        </div>
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>{formatBytes(item.file_size)}</span>
          <span>{formatDate(item.created_at)}</span>
        </div>
        {/* Three actions wrap on a narrow card rather than overflow it. */}
        <div className="flex flex-wrap justify-end gap-2">
          <LinkDialog item={item} onSubmit={saveLink} />
          <Button
            variant="outline"
            size="sm"
            aria-label={`Schedule ${item.file_name}`}
            onClick={(event) => onSchedule(item, event.currentTarget)}
          >
            Schedule…
          </Button>
          <Button
            variant={autoApprove ? "default" : "outline"}
            size="sm"
            aria-pressed={autoApprove}
            aria-label={`${RULE_LABELS.item} ${item.file_name}`}
            title={autoApproveHint(item, apiPublishing)}
            // Held with `aria-disabled`, not `disabled`, while its answer or
            // the re-read is awaited: a button that becomes disabled loses focus.
            aria-disabled={pending}
            onClick={toggleAutoApprove}
          >
            {RULE_LABELS.item}
          </Button>
        </div>
        {notice && (
          <p role="alert" className="text-xs text-destructive">
            {notice}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
