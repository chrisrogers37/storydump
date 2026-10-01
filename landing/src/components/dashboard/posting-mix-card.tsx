import { Layers } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/dashboard/empty-state";
import type { FolderMixView, FolderMix } from "@/lib/dashboard-payloads";

/**
 * The posting mix beside what it produced: one row per connected folder, two
 * columns named once in the header and defined once in the footer. Target is
 * the share of posts today's mix plans for the folder (the category-mix
 * route's `effective`, which Settings shows as "posts about"). Posted is its
 * share of the posts the mix picked in the stats window
 * (`stats.posted_by_source`, cadence stories only), so a story a person
 * planned is left out, and the footer says so.
 *
 * A window with no post, or an API that sent no counts, has no posted share:
 * `deriveFolderMix` types it `Unavailable`, the cell prints "—", and the
 * footer says which.
 */
export function PostingMixCard({ mix }: { mix: FolderMix }) {
  const { folders, total, fromRemoved } = mix;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Posting mix</CardTitle>
      </CardHeader>
      <CardContent>
        {folders.length === 0 ? (
          <EmptyState
            icon={Layers}
            title="No folders connected yet"
            description="Connect a Google Drive folder under Integrations in Settings."
          />
        ) : (
          <div className="space-y-3">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b text-xs text-muted-foreground">
                  <th scope="col" className="w-full pb-2 text-left font-normal">
                    Folder
                  </th>
                  <th scope="col" className="pb-2 pl-4 text-right font-normal">
                    Target
                  </th>
                  <th scope="col" className="pb-2 pl-4 text-right font-normal">
                    Posted
                  </th>
                </tr>
              </thead>
              <tbody>
                {folders.map((f) => (
                  <tr key={f.sourceId}>
                    <th scope="row" className="break-words pt-2 text-left font-medium">
                      {f.name}
                    </th>
                    <td className="whitespace-nowrap pt-2 pl-4 text-right tabular-nums">{target(f)}</td>
                    <td className="whitespace-nowrap pt-2 pl-4 text-right tabular-nums">
                      {f.posted === null ? "—" : `${f.posted.toFixed(0)}%`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            <p className="border-t pt-3 text-xs text-muted-foreground">
              Target: today&apos;s mix.{" "}
              {total === null
                ? "Posted shares are not available yet."
                : total === 0
                  ? "Nothing posted in this window yet."
                  : `Posted: each folder's share of the ${total} ${total === 1 ? "post" : "posts"} the mix picked in this window${
                      fromRemoved > 0
                        ? `, ${fromRemoved} of them from folders no longer connected`
                        : ""
                    }. Stories you planned yourself are left out.`}
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** What the folder is set to: its share, marked "auto" when the mix derives it, or Off. */
function target(f: FolderMixView): string {
  if (f.mode === "off") return "Off";
  const share = `${f.planned.toFixed(0)}%`;
  return f.mode === "automatic" ? `${share} auto` : share;
}
