import { afterEach, describe, expect, it, vi } from "vitest";
import { evenSplit, percentRows, saveCategoryMix, toMix } from "./category-mix";

const WS = "11111111-1111-4111-8111-111111111111";

describe("percentRows — what the card shows", () => {
  it("unions the folders the sync found with the categories already weighted", () => {
    const rows = percentRows(
      { mix: [{ category: "memes", ratio: 0.7 }, { category: "old", ratio: 0.3 }] },
      { categories: [{ category: "memes", media_count: 12 }, { category: "merch", media_count: 4 }, { category: null, media_count: 2 }] },
    );
    expect(rows).toEqual([
      { category: "memes", percent: 70, mediaCount: 12, discovered: true },
      { category: "merch", percent: 0, mediaCount: 4, discovered: true },
      { category: "old", percent: 30, mediaCount: 0, discovered: false },
    ]);
  });
  it("leaves the root's uncategorized files out of the weighted rows", () => {
    const rows = percentRows({ mix: [] }, { categories: [{ category: null, media_count: 9 }] });
    expect(rows).toEqual([]);
  });
});

describe("toMix — the card's numbers become the API's ratios", () => {
  const rows = (...percents: number[]) =>
    percents.map((p, i) => ({ category: `c${i}`, percent: p, mediaCount: 1, discovered: true }));
  it("converts percentages summing to 100 into ratios summing to 1", () => {
    expect(toMix(rows(70, 30))).toEqual({ ok: true, mix: [{ category: "c0", ratio: 0.7 }, { category: "c1", ratio: 0.3 }] });
  });
  it("tolerates a thirds split", () => {
    const out = toMix(rows(33.3, 33.3, 33.4));
    expect(out.ok).toBe(true);
  });
  it("refuses a total that is not 100", () => {
    expect(toMix(rows(70, 20))).toEqual({ ok: false, error: "sum_not_100", total: 90 });
  });
  it("refuses a negative or non-numeric entry by name", () => {
    expect(toMix(rows(110, -10)).ok).toBe(false);
    expect(toMix([{ category: "a", percent: Number.NaN, mediaCount: 1, discovered: true }]).ok).toBe(false);
  });
  it("all zeros clears the weighting", () => {
    expect(toMix(rows(0, 0))).toEqual({ ok: true, mix: [] });
  });
});

describe("evenSplit", () => {
  it("splits to one decimal and gives the remainder to the first row", () => {
    const out = evenSplit(3);
    expect(out.reduce((a, b) => a + b, 0)).toBeCloseTo(100, 5);
    expect(out[0]).toBeGreaterThanOrEqual(out[1]);
  });
});

describe("saveCategoryMix", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("PUTs the mix to the workspace's resource", async () => {
    const captured: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        captured.push({ url, init });
        return new Response(JSON.stringify({ mix: [{ category: "memes", ratio: 1 }] }), { status: 200, headers: { "Content-Type": "application/json" } });
      }),
    );
    const result = await saveCategoryMix(WS, [{ category: "memes", ratio: 1 }]);
    expect(result.ok).toBe(true);
    expect(captured[0].url).toBe(`/api/workspaces/${WS}/category-mix`);
    expect(captured[0].init?.method).toBe("PUT");
    expect(JSON.parse(String(captured[0].init?.body))).toEqual({ mix: [{ category: "memes", ratio: 1 }] });
  });
});
