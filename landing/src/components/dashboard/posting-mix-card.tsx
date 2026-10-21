import { Layers } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/dashboard/empty-state";
import { wholeShares } from "@/lib/category-mix";
import type { FolderMixView, FolderMix, SourceShares } from "@/lib/dashboard-payloads";
import { LONG_RUN_COPY } from "@/lib/source-mix";

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
 *
 * Each column is rounded as a whole (`wholeShares`), so the percents a reader
 * adds up come to what the shares come to: 100, or less when some of the
 * window's posts are from folders no longer connected.
 */
export function PostingMixCard({ mix }: { mix: FolderMix }) {
  const { folders, total, fromRemoved } = mix;
  const targets = wholeShares(folders.map((f) => (f.mode === "off" ? 0 : f.planned)));
  const posted = wholeShares(folders.map((f) => f.posted ?? 0));

  return (
    <Card>
      <CardHeader>
        <CardTitle>Posting mix</CardTitle>
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
                {folders.map((f, i) => (
                  <tr key={f.sourceId}>
                    <th scope="row" className="break-words pt-2 text-left font-medium">
                      {f.name}
                    </th>
                    <td className="whitespace-nowrap pt-2 pl-4 text-right tabular-nums">
                      {target(f, targets[i])}
                    </td>
                    <td className="whitespace-nowrap pt-2 pl-4 text-right tabular-nums">
                      {f.posted === null ? "—" : `${posted[i]}%`}
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

/** What the folder is set to: its whole share, marked "auto" when the mix derives it, or Off. */
function target(f: FolderMixView, planned: number): string {
  if (f.mode === "off") return "Off";
  return f.mode === "automatic" ? `${planned}% auto` : `${planned}%`;
}

/**
 * The same card for a converted workspace (protocol 2): one row per SOURCE —
 * Instagram past posts and Drive folders alike, each with its provider — and its
 * workspace target as the settings card states it, Off as Off. The posted
 * column is counted as above. `PostingMixCard` is the card for protocol 1;
 * the overview picks between them by the read's protocol.
 *
 * Its empty states say which empty it is: no source at all (connect one: an
 * account's past posts or a Drive folder), every source deliberately Off (a
 * choice, said as one), or a mix this page could not read (`null`), which is
 * never drawn as "nothing connected".
 */
export function SourcePostingMixCard({ mix }: { mix: SourceShares | null }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Posting mix</CardTitle>
      </CardHeader>
      <CardContent>
        {mix === null ? (
          <p className="text-sm text-muted-foreground">
            The posting mix could not be read. It is shown in Settings, under Posting mix.
          </p>
        ) : mix.sources.length === 0 ? (
          <EmptyState
            icon={Layers}
            title="No content sources yet"
            description="Turn on an Instagram account's past posts in Posting mix, or connect a Google Drive folder under Integrations in Settings."
          />
        ) : (
          <SourceShareTable mix={mix} />
        )}
      </CardContent>
    </Card>
  );
}

function SourceShareTable({ mix }: { mix: SourceShares }) {
  const { sources, total, fromRemoved, allOff, accountsDiffer } = mix;
  const posted = wholeShares(sources.map((s) => s.posted ?? 0));
  return (
    <div className="space-y-3">
      {allOff && (
        <p className="text-sm text-muted-foreground">
          Every source is Off, so nothing is drawn until one is turned on in Posting mix.
        </p>
      )}
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b text-xs text-muted-foreground">
            <th scope="col" className="w-full pb-2 text-left font-normal">
              Source
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
          {sources.map((s, i) => (
            <tr key={s.sourceId}>
              <th scope="row" className="break-words pt-2 text-left font-medium">
                {s.title}
                <span className="block text-xs font-normal text-muted-foreground">{s.subtitle}</span>
              </th>
              <td className="whitespace-nowrap pt-2 pl-4 text-right tabular-nums">{s.target}</td>
              <td className="whitespace-nowrap pt-2 pl-4 text-right tabular-nums">
                {s.posted === null ? "—" : `${posted[i]}%`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <p className="border-t pt-3 text-xs text-muted-foreground">
        Target: the workspace&apos;s target shares. {LONG_RUN_COPY}
        {accountsDiffer ? " Each account's own share can differ; Posting mix shows it." : ""}{" "}
        {total === null
          ? "Posted shares are not available yet."
          : total === 0
            ? "Nothing posted in this window yet."
            : `Posted: each source's share of the ${total} ${total === 1 ? "post" : "posts"} the mix picked in this window${
                fromRemoved > 0
                  ? `, ${fromRemoved} of them from sources no longer connected`
                  : ""
              }. Stories you planned yourself are left out.`}
      </p>
    </div>
  );
}
