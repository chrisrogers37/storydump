"use client";

import { useMemo, useRef, useState, type ReactNode, type Ref } from "react";
import { ImageOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/dashboard/empty-state";
import { MediaCard } from "@/components/dashboard/media/media-card";
import { ScheduleDialog, type ScheduleTargets } from "@/components/dashboard/media/schedule-dialog";
import {
  ARCHIVE_PAGE_SIZE,
  archiveProvenance,
  emptyLibrary,
  isArchiveMedia,
  pageOf,
  withArchiveName,
  type PastPosts,
} from "@/lib/archive-media";
import type { MediaRow } from "@/lib/dashboard-payloads";
import { formatInZone, instant } from "@/lib/zoned-dates";

/**
 * The media pool (#1044 `GET …/media?state=&never_posted=&limit=`).
 *
 * ── No server paging, because the route has no offset ──────────────────────
 *
 * The route takes `limit` ONLY — no offset, no category parameter — so
 * neither server paging nor a server-side category filter is expressible
 * against it. Rather than fake a page count from a bounded list, the grid asks
 * for one bounded page and SAYS SO: the bound is rendered, not implied.
 * Filtering is over that fetched set for the same reason, and the label says
 * which set it filtered.
 *
 * That is deliberate: inventing `total` from a truncated list is the
 * "confident wrong figure" (#1044), and a Next button that silently returns
 * the same twenty items is worse than no Next button. Either needs an offset
 * and a category filter on the route (#1048).
 *
 * Each item can be scheduled onto an account at a chosen time (#1413).
 * One dialog serves the grid, opened for the item whose Schedule… was pressed.
 * Each item can also carry the link its stories ask a person to add by hand
 * (#1413), set from its own Link…; the card shows it under the name.
 * And each can be approved automatically whenever it is served (#1477), from
 * its own Auto-approve; the card's 🔁 badge says it is on (`MediaCard`).
 *
 * An item from a connected account's past posts (#1644) goes by its own
 * name and says where it came from, and never carries Instagram's id. Its
 * picture is asked for under the archive's limits (`ArchiveThumbnail`), and a
 * library that holds any is listed 24 to a page: a page is a window over the
 * rows the one bounded read returned, so no more pictures than that are drawn
 * at once. The card is `MediaCard`'s (`media-card.tsx`); this file holds the
 * grid around it.
 */
/** The pages of a library that holds archive items: Previous, where the reader is, Next. */
export function GridPager({
  page,
  pages,
  onPage,
}: {
  page: number;
  pages: number;
  onPage: (page: number) => void;
}) {
  return (
    <nav aria-label="Library pages" className="flex items-center justify-center gap-3">
      <Button
        variant="outline"
        size="sm"
        disabled={page <= 0}
        onClick={() => onPage(page - 1)}
      >
        Previous
      </Button>
      <span className="text-sm text-muted-foreground" aria-live="polite">
        Page {page + 1} of {pages}
      </span>
      <Button
        variant="outline"
        size="sm"
        disabled={page >= pages - 1}
        onClick={() => onPage(page + 1)}
      >
        Next
      </Button>
    </nav>
  );
}

/**
 * The grid from what it is shown: the rows (an archive item already under its
 * own name), the category and page chosen, and what a press does. It holds no
 * state, so a test draws any page or category as it is; `MediaGrid` holds them.
 *
 * A library with archive items is listed a page at a time, so only a page of
 * their pictures is ever on screen to be asked for; one without is listed whole.
 */
export function MediaGridView({
  items,
  limit,
  workspaceId,
  tz,
  pastPosts,
  apiPublishing,
  category,
  page,
  gridRef,
  onCategory,
  onPage,
  onSchedule,
  children,
}: {
  items: MediaRow[];
  limit: number;
  workspaceId: string;
  /** The workspace's zone, which the day an archive item was posted is read on. */
  tz: string;
  /** Whether the workspace has past posts as a source, which an empty library says. */
  pastPosts: PastPosts | null;
  /** Whether the workspace posts through the Instagram API; null when it could not be read. */
  apiPublishing: boolean | null;
  category: string | null;
  page: number;
  gridRef?: Ref<HTMLDivElement>;
  onCategory: (category: string | null) => void;
  onPage: (page: number) => void;
  onSchedule: (item: MediaRow, opener: HTMLButtonElement) => void;
  /** Drawn last, after the note under the grid: the schedule dialog. */
  children?: ReactNode;
}) {
  const counts = new Map<string, number>();
  for (const item of items) {
    if (!item.category) continue;
    counts.set(item.category, (counts.get(item.category) ?? 0) + 1);
  }
  const categories = [...counts.entries()].sort(([a], [b]) => a.localeCompare(b));

  const shown = category
    ? items.filter((i) => i.category === category)
    : items;

  const empty = emptyLibrary(pastPosts);
  const paged = shown.some(isArchiveMedia);
  const listing = paged
    ? pageOf(shown, page)
    : { rows: shown, page: 0, pages: 1 };
  const first = listing.page * ARCHIVE_PAGE_SIZE + 1;
  const last = listing.page * ARCHIVE_PAGE_SIZE + listing.rows.length;

  // The list is bounded, so it may be a prefix of the library rather than all
  // of it. A reader cannot tell those apart from the grid alone.
  const atBound = items.length >= limit;
  // Keep the read's order for standalone items. A post group takes the place
  // of its first slide; its other slides join it even if the read interleaves them.
  const grouped: { key: string; slides: MediaRow[] }[] = [];
  const byPost = new Map<string, { key: string; slides: MediaRow[] }>();
  for (const item of listing.rows) {
    const slide = archiveProvenance(item)?.slide;
    if (!slide) {
      grouped.push({ key: item.id, slides: [item] });
      continue;
    }
    const key = item.source_id
      ? `${workspaceId}:${item.source_id}:${slide.post_ref}`
      : `${workspaceId}:${item.id}`;
    let group = byPost.get(key);
    if (!group) {
      group = { key, slides: [] };
      byPost.set(key, group);
      grouped.push(group);
    }
    group.slides.push(item);
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        <Button
          variant={category === null ? "default" : "outline"}
          size="sm"
          onClick={() => onCategory(null)}
        >
          All ({items.length})
        </Button>
        {categories.map(([name, count]) => (
          <Button
            key={name}
            variant={category === name ? "default" : "outline"}
            size="sm"
            onClick={() => onCategory(name)}
          >
            {name} ({count})
          </Button>
        ))}
      </div>

      <div
        ref={gridRef}
        className="grid scroll-mt-6 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4"
      >
        {shown.length === 0 ? (
          <div className="col-span-full">
            <EmptyState
              icon={ImageOff}
              title={empty.title}
              description={empty.description}
              action={empty.action}
            />
          </div>
        ) : (
          grouped.map((group) => {
            // Keyed on the item alone: a re-read keeps the card, and the
            // focus in it, and the card takes the server's flag in place.
            const card = (item: MediaRow) => (
              <MediaCard
                key={item.id}
                item={item}
                workspaceId={workspaceId}
                tz={tz}
                apiPublishing={apiPublishing}
                onSchedule={onSchedule}
              />
            );
            const firstItem = group.slides[0];
            const origin = archiveProvenance(firstItem);
            const slide = origin?.slide;
            if (!origin || !slide) return card(firstItem);
            const day = origin.original_at && !Number.isNaN(instant(origin.original_at).getTime())
              ? formatInZone(origin.original_at, tz, { month: "short", day: "numeric", year: "numeric" })
              : null;
            return (
              <section key={group.key} className="col-span-full space-y-2" aria-label="Instagram post slides">
                <h3 className="text-sm font-semibold">
                  Instagram post{origin.origin_handle ? ` · ${origin.origin_handle}` : ""}{day ? ` · ${day}` : ""}{slide.count !== null ? ` · ${slide.count} slides` : ""}
                </h3>
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                  {group.slides.map(card)}
                </div>
              </section>
            );
          })
        )}
      </div>

      {paged && <GridPager page={listing.page} pages={listing.pages} onPage={onPage} />}

      <p className="pt-2 text-xs text-muted-foreground">
        {paged
          ? `Showing ${first}–${last} of ${shown.length} loaded items${category ? ` in "${category}"` : ""}.`
          : category
            ? `Showing ${shown.length} of ${items.length} loaded items in "${category}".`
            : `Showing ${items.length} items.`}{" "}
        {atBound
          ? `This is the first ${limit} in the library — the API serves a bounded list with no page control, so there may be more.`
          : "That is the whole library."}
      </p>

      {children}
    </div>
  );
}

export function MediaGrid({
  items: rows,
  limit,
  workspaceId,
  targets,
  apiPublishing,
  pastPosts = null,
}: {
  items: MediaRow[];
  limit: number;
  workspaceId: string;
  targets: ScheduleTargets;
  /** Whether the workspace posts through the Instagram API; null when it could not be read. */
  apiPublishing: boolean | null;
  /** The workspace's past-posts source, from its mix; null when it has none or the mix is not one. */
  pastPosts?: PastPosts | null;
}) {
  const [category, setCategory] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const [scheduling, setScheduling] = useState<MediaRow | null>(null);
  // The Schedule… that opened the dialog, where focus goes back on close.
  const opener = useRef<HTMLButtonElement | null>(null);
  const gridTop = useRef<HTMLDivElement | null>(null);

  // An archive item is drawn under its own name from here down, dialogs included.
  const items = useMemo(() => rows.map(withArchiveName), [rows]);

  return (
    <MediaGridView
      items={items}
      limit={limit}
      workspaceId={workspaceId}
      tz={targets?.workspaceTz ?? "UTC"}
      pastPosts={pastPosts}
      apiPublishing={apiPublishing}
      category={category}
      page={page}
      gridRef={gridTop}
      onCategory={(next) => {
        setCategory(next);
        setPage(0);
      }}
      onPage={(next) => {
        setPage(next);
        // The new page starts at its top, not wherever the last one was left.
        gridTop.current?.scrollIntoView?.({ block: "start" });
      }}
      onSchedule={(item, button) => {
        opener.current = button;
        setScheduling(item);
      }}
    >
      <ScheduleDialog
        workspaceId={workspaceId}
        item={scheduling}
        targets={targets}
        onClose={() => setScheduling(null)}
        returnFocusTo={opener}
      />
    </MediaGridView>
  );
}
