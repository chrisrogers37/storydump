"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import type {
  CardRow,
  CategoryMixResponse,
  WeightMode,
} from "@/lib/category-mix";
import {
  cardRows,
  evenSplit,
  mixRefusalCopy,
  saveCategoryMix,
  toMixBySource,
} from "@/lib/category-mix";

/**
 * How often each CONNECTED FOLDER posts (owner ruling 2026-09-08: sources are
 * the groups). A folder is explicit (a percentage), Automatic (it posts in
 * proportion to its files, together with the other automatic folders never
 * more than the smallest explicit weight), or Off (stays synced, never
 * posts). "Posts about" is the API's number — the share the planner draws —
 * not something this card computes.
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
  // The server's picture, re-derived when it changes (a refresh after a
  // save, a newly connected folder); a person mid-edit before a refresh keeps
  // nothing, which is the honest outcome — the numbers on screen are the
  // server's again.
  const seed = data ? JSON.stringify(data.rows) : "";
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const baseline = useMemo(() => (data ? cardRows(data) : []), [seed]);
  const [rows, setRows] = useState<CardRow[]>(baseline);
  useEffect(() => setRows(baseline), [baseline]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const explicitTotal =
    Math.round(
      rows
        .filter((r) => r.mode === "explicit")
        .reduce((a, r) => a + r.percent, 0) * 10,
    ) / 10;
  const parsed = toMixBySource(rows);
  const dirty = rows !== baseline;

  function update(index: number, patch: Partial<CardRow>) {
    const next = rows.slice();
    next[index] = { ...next[index], ...patch };
    setRows(next);
  }

  function setMode(index: number, mode: WeightMode) {
    update(index, {
      mode,
      percent: mode === "explicit" ? rows[index].percent : 0,
    });
  }

  function splitEvenly() {
    const parts = evenSplit(rows.length);
    setRows(
      rows.map((r, i) => ({ ...r, mode: "explicit", percent: parts[i] })),
    );
  }

  function allAutomatic() {
    setRows(rows.map((r) => ({ ...r, mode: "automatic", percent: 0 })));
  }

  async function save() {
    setError(null);
    setNotice(null);
    if (!parsed.ok) {
      setError(
        parsed.error === "sum_not_100"
          ? `The percentages add up to ${parsed.total}, not 100. Folders set to Automatic take the rest on their own.`
          : parsed.error === "all_off"
            ? mixRefusalCopy("invalid_mix_all_off")
            : "Every percentage must be a number between 0 and 100.",
      );
      return;
    }
    setSaving(true);
    const result = await saveCategoryMix(workspaceId, parsed.rows);
    setSaving(false);
    if (!result.ok) {
      setError(mixRefusalCopy(result.error, result.status));
      return;
    }
    setNotice(
      parsed.rows.length === 0
        ? "Every folder is automatic: each posts in proportion to its files."
        : "Posting mix saved. It applies from the next posting slot.",
    );
    router.refresh();
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Posting mix</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          Each connected folder is a group. Give folders a share of posts, or
          leave them Automatic and they post in proportion to their files. Off
          keeps a folder synced but never posts from it. Subfolders are just
          structure — to weight two subfolders, connect them as folders. Rename
          folders freely; the weight follows the folder.
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
            No folders connected yet. Connect a Google Drive folder under
            Integrations; it appears here right away.
          </p>
        ) : (
          <ul className="divide-y">
            {rows.map((row, i) => (
              <li
                key={row.sourceId}
                className="flex flex-wrap items-center justify-between gap-3 py-2"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{row.name}</p>
                  <p className="text-xs text-muted-foreground">
                    {row.mediaCount} {row.mediaCount === 1 ? "file" : "files"}
                    {row.state !== "active" ? " · sync paused" : ""}
                    {!dirty ? ` · posts about ${row.effective}%` : ""}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <select
                    className="h-9 rounded-md border bg-background px-2 text-sm"
                    value={row.mode}
                    disabled={!editable}
                    onChange={(e) => setMode(i, e.target.value as WeightMode)}
                    aria-label={`${row.name} weighting`}
                  >
                    <option value="automatic">Automatic</option>
                    <option value="explicit">Weight</option>
                    <option value="off">Off</option>
                  </select>
                  {row.mode === "explicit" && (
                    <>
                      <Input
                        type="number"
                        min={0}
                        max={100}
                        step={0.1}
                        value={row.percent}
                        disabled={!editable}
                        onChange={(e) =>
                          update(i, {
                            percent:
                              e.target.value === ""
                                ? 0
                                : Number(e.target.value),
                          })
                        }
                        className="w-24"
                        aria-label={`${row.name} percent`}
                      />
                      <span className="text-sm text-muted-foreground">%</span>
                    </>
                  )}
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
              {rows.some((r) => r.mode === "explicit")
                ? `Your weights total ${explicitTotal}%${parsed.ok ? "" : " — must be 100"}`
                : "Every folder is automatic"}
              {dirty ? " · unsaved" : ""}
            </p>
            {editable && (
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={allAutomatic}
                  disabled={saving}
                >
                  Automatic for all
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={splitEvenly}
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
      </CardContent>
    </Card>
  );
}
