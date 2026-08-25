"use client";

import { useMemo, useState } from "react";
import { ImageOff } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/dashboard/empty-state";
import { Card, CardContent } from "@/components/ui/card";
import type { MediaRow } from "@/lib/dashboard-payloads";

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
    <Badge className="bg-green-600 text-xs hover:bg-green-700">
      {times}x posted
    </Badge>
  );
}

export function MediaGrid({
  items,
  limit,
}: {
  items: MediaRow[];
  limit: number;
}) {
  const [category, setCategory] = useState<string | null>(null);

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
              description="Connect Google Drive to sync your content library."
            />
          </div>
        ) : (
          shown.map((item) => (
            <Card key={item.id} className="overflow-hidden">
              <div className="relative flex h-32 items-center justify-center overflow-hidden bg-muted text-muted-foreground">
                {item.thumbnail_url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={item.thumbnail_url}
                    alt={item.file_name}
                    loading="lazy"
                    className="absolute inset-0 h-full w-full object-cover"
                    onError={(e) => {
                      // A stored Drive URL rotates; the next sync refreshes it.
                      // Fall back to the kind label rather than a broken image.
                      (e.currentTarget as HTMLImageElement).style.display = "none";
                    }}
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
    </div>
  );
}
