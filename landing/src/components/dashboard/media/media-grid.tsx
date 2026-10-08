"use client";

import { useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ImageOff } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/dashboard/empty-state";
import { ItemLink } from "@/components/dashboard/item-link";
import { LinkDialog } from "@/components/dashboard/media/link-dialog";
import { MediaThumbnail } from "@/components/dashboard/media/media-thumbnail";
import { ScheduleDialog, type ScheduleTargets } from "@/components/dashboard/media/schedule-dialog";
import { TONE_CLASS } from "@/components/dashboard/tone";
import { Card, CardContent } from "@/components/ui/card";
import { linkRefusalCopy, submitSetItemLink } from "@/lib/command-client";
import type { MediaRow } from "@/lib/dashboard-payloads";
import { thumbnailSrc } from "@/lib/thumbnails";

/**
 * The media pool (#1044 `GET …/media?state=&never_posted=&limit=`).
 *
 * ── Server paging is gone because the route has no offset ──────────────────
 *
 * The legacy grid paged server-side (`page`, `page_size`, `total`) and filtered
 * by category server-side. The target route takes `limit` ONLY — no offset, no
 * category parameter — so neither is expressible against it. Rather than fake
 * a page count from a bounded list, the grid asks for one bounded page and
 * SAYS SO: the bound is rendered, not implied. Filtering is over that fetched
 * set for the same reason, and the label says which set it filtered.
 *
 * That is a real reduction in what this screen can do and it is deliberate:
 * inventing `total` from a truncated list is the "confident wrong figure"
 * #1044 exists to stop, and a Next button that silently returns the same
 * twenty items is worse than no Next button. Restoring either needs an offset
 * and a category filter on the route — noted on #1048.
 *
 * Each item can be scheduled onto an account at a chosen time (#1413 phase 6).
 * One dialog serves the grid, opened for the item whose Schedule… was pressed.
 * Each item can also carry the link its stories ask a person to add by hand
 * (#1413 phase 7), set from its own Link…; the card shows it under the name.
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

export function MediaGrid({
  items,
  limit,
  workspaceId,
  targets,
}: {
  items: MediaRow[];
  limit: number;
  workspaceId: string;
  targets: ScheduleTargets;
}) {
  const [category, setCategory] = useState<string | null>(null);
  const [scheduling, setScheduling] = useState<MediaRow | null>(null);
  // The Schedule… that opened the dialog, where focus goes back on close.
  const opener = useRef<HTMLButtonElement | null>(null);
  const router = useRouter();

  // Sets or clears an item's link; answers the refusal's sentence, or null once saved.
  async function saveLink(item: MediaRow, link: string | null): Promise<string | null> {
    const result = await submitSetItemLink(workspaceId, item.id, link);
    if (result.ok) {
      router.refresh();
      return null;
    }
    if (result.status === 404) router.refresh();
    return linkRefusalCopy(result.error, result.status);
  }

  const categories = useMemo(() => {
    const counts = new Map<string, number>();
    for (const item of items) {
      if (!item.category) continue;
      counts.set(item.category, (counts.get(item.category) ?? 0) + 1);
    }
    return [...counts.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [items]);

  const shown = category
    ? items.filter((i) => i.category === category)
    : items;

  // The list is bounded, so it may be a prefix of the library rather than all
  // of it. A reader cannot tell those apart from the grid alone.
  const atBound = items.length >= limit;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        <Button
          variant={category === null ? "default" : "outline"}
          size="sm"
          onClick={() => setCategory(null)}
        >
          All ({items.length})
        </Button>
        {categories.map(([name, count]) => (
          <Button
            key={name}
            variant={category === name ? "default" : "outline"}
            size="sm"
            onClick={() => setCategory(name)}
          >
            {name} ({count})
          </Button>
        ))}
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {shown.length === 0 ? (
          <div className="col-span-full">
            <EmptyState
              icon={ImageOff}
              title="No media items found"
              description="Connect Google Drive and pick the folders to sync your content library."
              action={{ href: "/dashboard/settings?tab=integrations", label: "Set up Google Drive" }}
            />
          </div>
        ) : (
          shown.map((item) => (
            <Card key={item.id} className="overflow-hidden">
              <div className="relative flex h-32 items-center justify-center overflow-hidden bg-muted text-muted-foreground">
                {item.has_thumbnail ? (
                  <MediaThumbnail
                    key={item.thumbnail_version}
                    src={thumbnailSrc(workspaceId, item.id, item.thumbnail_version)}
                    alt={item.file_name}
                    video={item.media_kind === "video"}
                    // The kind label below is drawn under the picture, so a
                    // thumbnail that fails leaves it showing.
                    fallback={null}
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
                {item.link_url && <ItemLink link={item.link_url} />}
                <div className="flex items-center justify-between">
                  <Badge variant="outline" className="text-xs">
                    {item.category ?? "uncategorised"}
                  </Badge>
                  {postingBadge(item.times_posted)}
                </div>
                <div className="flex items-center justify-between text-xs text-muted-foreground">
                  <span>{formatBytes(item.file_size)}</span>
                  <span>{formatDate(item.created_at)}</span>
                </div>
                <div className="flex justify-end gap-2">
                  <LinkDialog item={item} onSubmit={saveLink} />
                  <Button
                    variant="outline"
                    size="sm"
                    aria-label={`Schedule ${item.file_name}`}
                    onClick={(event) => {
                      opener.current = event.currentTarget;
                      setScheduling(item);
                    }}
                  >
                    Schedule…
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))
        )}
      </div>

      <p className="pt-2 text-xs text-muted-foreground">
        {category
          ? `Showing ${shown.length} of ${items.length} loaded items in "${category}".`
          : `Showing ${items.length} items.`}{" "}
        {atBound
          ? `This is the first ${limit} in the library — the API serves a bounded list with no page control, so there may be more.`
          : "That is the whole library."}
      </p>

      <ScheduleDialog
        workspaceId={workspaceId}
        item={scheduling}
        targets={targets}
        onClose={() => setScheduling(null)}
        returnFocusTo={opener}
      />
    </div>
  );
}
