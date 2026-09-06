import { notAuthenticatedCopy, unreachableCopy } from "./refusal-copy";

/**
 * The category mix — how often each category posts (owner ruling
 * 2026-09-06: memes 70 / merch 30). Categories are the SUBFOLDERS of a picked
 * Drive folder, tagged by the sync; the weights live in the workspace's mix
 * table and the slot planner draws by them. Files directly in the picked
 * folder have no category and post only when no weighted category has media.
 */

export type MixRow = { category: string; ratio: number };
export type DiscoveredCategory = { category: string | null; media_count: number };
export type CategoryMixResponse = { mix: MixRow[]; categories: DiscoveredCategory[] };

export type PercentRow = {
  category: string;
  percent: number;
  mediaCount: number;
  /** false = weighted earlier but no folder of that name has media now. */
  discovered: boolean;
};

/** The card's rows: every discovered subfolder, plus any weighted category the sync no longer sees. */
export function percentRows(
  mix: { mix: MixRow[] },
  found: { categories: DiscoveredCategory[] },
): PercentRow[] {
  const weights = new Map(mix.mix.map((m) => [m.category, m.ratio]));
  const rows: PercentRow[] = [];
  for (const c of found.categories) {
    if (c.category === null) continue;
    rows.push({
      category: c.category,
      percent: round1((weights.get(c.category) ?? 0) * 100),
      mediaCount: c.media_count,
      discovered: true,
    });
  }
  const seen = new Set(rows.map((r) => r.category));
  for (const m of mix.mix) {
    if (!seen.has(m.category)) {
      rows.push({ category: m.category, percent: round1(m.ratio * 100), mediaCount: 0, discovered: false });
    }
  }
  return rows;
}

export type ToMixResult =
  | { ok: true; mix: MixRow[] }
  | { ok: false; error: "sum_not_100" | "bad_percent"; total?: number };

/** Percentages → ratios. All zeros means "no weighting"; otherwise the total must be 100. */
export function toMix(rows: PercentRow[]): ToMixResult {
  for (const r of rows) {
    if (!Number.isFinite(r.percent) || r.percent < 0 || r.percent > 100) {
      return { ok: false, error: "bad_percent" };
    }
  }
  const total = rows.reduce((a, r) => a + r.percent, 0);
  if (total === 0) return { ok: true, mix: [] };
  if (Math.abs(total - 100) > 0.1) return { ok: false, error: "sum_not_100", total: round1(total) };
  return {
    ok: true,
    mix: rows.filter((r) => r.percent > 0).map((r) => ({ category: r.category, ratio: Math.round(r.percent * 100) / 10000 })),
  };
}

/** An even split to one decimal; the remainder lands on the first row so the total is exactly 100. */
export function evenSplit(count: number): number[] {
  if (count <= 0) return [];
  const each = Math.floor((100 / count) * 10) / 10;
  const out = Array.from({ length: count }, () => each);
  out[0] = round1(100 - each * (count - 1));
  return out;
}

function round1(n: number): number {
  return Math.round(n * 10) / 10;
}

export type SaveMixResult = { ok: true; mix: MixRow[] } | { ok: false; error: string; status: number };

export async function saveCategoryMix(workspaceId: string, mix: MixRow[]): Promise<SaveMixResult> {
  let response: Response;
  try {
    response = await fetch(`/api/workspaces/${workspaceId}/category-mix`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mix }),
    });
  } catch {
    return { ok: false, error: "unreachable", status: 0 };
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = typeof data?.error === "string" ? data.error : `http_${response.status}`;
    return { ok: false, error, status: response.status };
  }
  return { ok: true, mix: Array.isArray(data?.mix) ? data.mix : mix };
}

export function mixRefusalCopy(reason: unknown, status?: number): string {
  if (status === 403 || reason === "http_403") {
    return "You need to be an admin of this workspace to change the mix.";
  }
  if (typeof reason === "string" && reason.startsWith("invalid_mix_")) {
    return "The mix was refused: percentages must be between 0 and 100 and add up to 100. Nothing changed.";
  }
  switch (reason) {
    case "unauthenticated":
    case "http_401":
      return notAuthenticatedCopy("Nothing changed.");
    case "unreachable":
    case "target_router_unreachable":
      return unreachableCopy("Nothing changed");
  }
  return "Could not save the mix. Nothing changed — try again shortly.";
}
