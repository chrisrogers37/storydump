"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import type { CategoryMixResponse, PercentRow } from "@/lib/category-mix";
import {
  evenSplit,
  mixRefusalCopy,
  percentRows,
  saveCategoryMix,
  toMix,
} from "@/lib/category-mix";

/**
 * How often each category posts (owner ruling 2026-09-06). The rows are the
 * SUBFOLDERS the sync found under the workspace's Drive folders; the numbers
 * are percentages that must add up to 100, or all zeros for no weighting.
 */
export function CategoryWeightsCard({
  workspaceId,
  data,
  editable,
}: {
  workspaceId: string;
  data: CategoryMixResponse | null;
  editable: boolean;
}) {
  const router = useRouter();
  const [rows, setRows] = useState<PercentRow[]>(() =>
    data ? percentRows(data, data) : [],
  );
  // Re-seed when the server's picture changes (a refresh after a save, a
  // newly discovered folder); a person mid-edit before a refresh keeps
  // nothing, which is the honest outcome — the numbers on screen are the
  // server's again.
  const seed = data ? JSON.stringify([data.mix, data.categories]) : "";
  useEffect(() => {
    setRows(data ? percentRows(data, data) : []);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seed]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const uncategorized =
    data?.categories.find((c) => c.category === null)?.media_count ?? 0;
  const total = Math.round(rows.reduce((a, r) => a + r.percent, 0) * 10) / 10;
  const parsed = toMix(rows);

  function setPercent(index: number, value: string) {
    const next = rows.slice();
    next[index] = { ...next[index], percent: value === "" ? 0 : Number(value) };
    setRows(next);
  }

  function split() {
    const parts = evenSplit(rows.length);
    setRows(rows.map((r, i) => ({ ...r, percent: parts[i] })));
  }

  async function save() {
    setError(null);
    setNotice(null);
    if (!parsed.ok) {
      setError(
        parsed.error === "sum_not_100"
          ? `The percentages add up to ${parsed.total}, not 100.`
          : "Every percentage must be a number between 0 and 100.",
      );
      return;
    }
    setSaving(true);
    const result = await saveCategoryMix(workspaceId, parsed.mix);
    setSaving(false);
    if (!result.ok) {
      setError(mixRefusalCopy(result.error, result.status));
      return;
    }
    setNotice(
      parsed.mix.length === 0
        ? "Weighting cleared: categories post in the order media arrived."
        : "Mix saved. It applies from the next posting slot.",
    );
    router.refresh();
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Category mix</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          Each subfolder directly inside a synced Drive folder is a category
          (one level: folders nested deeper are not walked). The percentages say
          how often each one posts; they must add up to 100, or all be 0 to post
          in arrival order. Renaming a folder in Drive leaves its weight under
          the old name — move the number to the new row.
        </p>
        {error && (
          <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
            {error}
          </div>
        )}
        {notice && (
          <div className="rounded-md border border-green-200 bg-green-50 p-3 text-sm text-green-800">
            {notice}
          </div>
        )}
        {data === null ? (
          <p className="text-sm text-muted-foreground">
            The mix could not be loaded just now. Reload to try again.
          </p>
        ) : rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No categories yet. Put your media in subfolders of the Drive folder
            you picked; each subfolder appears here after the next sync.
          </p>
        ) : (
          <ul className="divide-y">
            {rows.map((row, i) => (
              <li
                key={row.category}
                className="flex flex-wrap items-center justify-between gap-3 py-2"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{row.category}</p>
                  <p className="text-xs text-muted-foreground">
                    {row.discovered
                      ? `${row.mediaCount} ${row.mediaCount === 1 ? "file" : "files"} available`
                      : "No folder of this name has media right now"}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <Input
                    type="number"
                    min={0}
                    max={100}
                    step={0.1}
                    value={row.percent}
                    disabled={!editable}
                    onChange={(e) => setPercent(i, e.target.value)}
                    className="w-24"
                    aria-label={`${row.category} percent`}
                  />
                  <span className="text-sm text-muted-foreground">%</span>
                </div>
              </li>
            ))}
          </ul>
        )}
        {rows.length > 0 && (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p
              className={`text-sm ${parsed.ok ? "text-muted-foreground" : "text-amber-700"}`}
            >
              Total {total}%{parsed.ok ? "" : " — must be 100"}
            </p>
            {editable && (
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={split}
                  disabled={saving}
                >
                  Split evenly
                </Button>
                <Button
                  size="sm"
                  onClick={save}
                  disabled={saving || !parsed.ok}
                >
                  {saving ? "Saving..." : "Save mix"}
                </Button>
              </div>
            )}
          </div>
        )}
        {uncategorized > 0 && (
          <p className="text-xs text-muted-foreground">
            {uncategorized} {uncategorized === 1 ? "file sits" : "files sit"}{" "}
            directly in the folder with no subfolder. Those post only when no
            weighted category has media.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
