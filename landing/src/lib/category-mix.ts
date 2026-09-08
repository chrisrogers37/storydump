import { notAuthenticatedCopy, unreachableCopy } from "./refusal-copy";

/**
 * The posting mix — how often each CONNECTED FOLDER posts (owner ruling
 * 2026-09-08: sources are the groups). Everything inside a connected folder
 * syncs, at any depth; to weight two subfolders separately, connect each as
 * its own folder. A folder is explicit (a percentage), automatic (no row:
 * it posts in proportion to its files, together with the other automatic
 * folders never more than the smallest explicit weight), or Off (0: stays
 * connected and synced, never posts). The API computes the share each folder
 * actually gets (`effective`); the card only renders it.
 */

export type MixSourceRow = {
  source_id: string;
  provider: string;
  name: string;
  state: string;
  media_count: number;
  /** null = automatic; 0 = Off; otherwise the explicit fraction. */
  ratio: number | null;
  /** The share of posts this folder gets, in percent, as the planner draws it. */
  effective: number;
};
export type CategoryMixResponse = {
  rows: MixSourceRow[];
  explicit_total: number;
};
export type MixWrite = { source_id: string; ratio: number };

export type WeightMode = "explicit" | "automatic" | "off";
export type CardRow = {
  sourceId: string;
  name: string;
  mediaCount: number;
  state: string;
  mode: WeightMode;
  /** The typed percentage; meaningful for explicit rows only. */
  percent: number;
  effective: number;
};

/** The card's rows: every connected folder, with how it is weighted. */
export function cardRows(data: { rows: MixSourceRow[] }): CardRow[] {
  return data.rows.map((r) => ({
    sourceId: r.source_id,
    name: r.name,
    mediaCount: r.media_count,
    state: r.state,
    mode: r.ratio === null ? "automatic" : r.ratio === 0 ? "off" : "explicit",
    percent: r.ratio === null ? 0 : round1(r.ratio * 100),
    effective: r.effective,
  }));
}

export type ToMixResult =
  | { ok: true; rows: MixWrite[] }
  | {
      ok: false;
      error: "sum_not_100" | "bad_percent" | "all_off";
      total?: number;
    };

/**
 * The card's rows → the API's rows. Explicit percentages must add up to 100
 * (automatic rows are not in the sum: the API gives them the rest); an
 * explicit 0 is Off, kept, never dropped; automatic rows are left out; every
 * folder Off is refused — something must post.
 */
export function toMixBySource(rows: CardRow[]): ToMixResult {
  const explicit = rows.filter((r) => r.mode === "explicit");
  for (const r of explicit) {
    if (!Number.isFinite(r.percent) || r.percent < 0 || r.percent > 100) {
      return { ok: false, error: "bad_percent" };
    }
  }
  const positive = explicit.filter((r) => r.percent > 0);
  const total = positive.reduce((a, r) => a + r.percent, 0);
  if (positive.length > 0 && Math.abs(total - 100) > 0.1) {
    return { ok: false, error: "sum_not_100", total: round1(total) };
  }
  const off = rows.filter(
    (r) => r.mode === "off" || (r.mode === "explicit" && r.percent === 0),
  );
  const automatic = rows.filter((r) => r.mode === "automatic");
  if (rows.length > 0 && positive.length === 0 && automatic.length === 0) {
    return { ok: false, error: "all_off" };
  }
  return {
    ok: true,
    rows: [
      ...positive.map((r) => ({
        source_id: r.sourceId,
        ratio: Math.round(r.percent * 100) / 10000,
      })),
      ...off.map((r) => ({ source_id: r.sourceId, ratio: 0 })),
    ],
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

export type SaveMixResult =
  | { ok: true; rows: MixSourceRow[] }
  | { ok: false; error: string; status: number };

export async function saveCategoryMix(
  workspaceId: string,
  rows: MixWrite[],
): Promise<SaveMixResult> {
  let response: Response;
  try {
    response = await fetch(`/api/workspaces/${workspaceId}/category-mix`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rows }),
    });
  } catch {
    return { ok: false, error: "unreachable", status: 0 };
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error =
      typeof data?.error === "string" ? data.error : `http_${response.status}`;
    return { ok: false, error, status: response.status };
  }
  return { ok: true, rows: Array.isArray(data?.rows) ? data.rows : [] };
}

export function mixRefusalCopy(reason: unknown, status?: number): string {
  if (status === 403 || reason === "http_403") {
    return "You need to be an admin of this workspace to change the mix.";
  }
  switch (reason) {
    case "invalid_mix_unknown_source":
      return "One of these folders is no longer connected. Reload the page and try again. Nothing changed.";
    case "invalid_mix_all_off":
      return "At least one folder has to post: give a folder a percentage or set it to Automatic. Nothing changed.";
    case "unauthenticated":
    case "http_401":
      return notAuthenticatedCopy("Nothing changed.");
    case "unreachable":
    case "target_router_unreachable":
      return unreachableCopy("Nothing changed");
  }
  if (typeof reason === "string" && reason.startsWith("invalid_mix_")) {
    return "The mix was refused: percentages must be between 0 and 100 and add up to 100. Nothing changed.";
  }
  return "Could not save the mix. Nothing changed — try again shortly.";
}
