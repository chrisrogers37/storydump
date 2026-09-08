import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cardRows,
  evenSplit,
  mixRefusalCopy,
  saveCategoryMix,
  toMixBySource,
  type MixSourceRow,
} from "./category-mix";
import { addFolderRefusalCopy } from "./drive";

const WS = "11111111-1111-4111-8111-111111111111";
const S1 = "11111111-1111-4111-8111-aaaaaaaaaaaa";
const S2 = "22222222-2222-4222-8222-bbbbbbbbbbbb";
const S3 = "33333333-3333-4333-8333-cccccccccccc";

const row = (over: Partial<MixSourceRow>): MixSourceRow => ({
  source_id: S1,
  provider: "gdrive",
  name: "memes",
  state: "active",
  media_count: 10,
  ratio: null,
  effective: 0,
  ...over,
});

describe("cardRows — every connected folder, with how it is weighted", () => {
  it("reads explicit, automatic and off from the ratio", () => {
    const rows = cardRows({
      rows: [
        row({
          source_id: S1,
          name: "memes",
          ratio: 0.7,
          effective: 69.3,
          media_count: 30,
        }),
        row({
          source_id: S2,
          name: "events",
          ratio: null,
          effective: 1.0,
          media_count: 4,
        }),
        row({
          source_id: S3,
          name: "archive",
          ratio: 0,
          effective: 0,
          media_count: 500,
        }),
      ],
    });
    expect(rows).toEqual([
      {
        sourceId: S1,
        name: "memes",
        mediaCount: 30,
        state: "active",
        mode: "explicit",
        percent: 70,
        effective: 69.3,
      },
      {
        sourceId: S2,
        name: "events",
        mediaCount: 4,
        state: "active",
        mode: "automatic",
        percent: 0,
        effective: 1.0,
      },
      {
        sourceId: S3,
        name: "archive",
        mediaCount: 500,
        state: "active",
        mode: "off",
        percent: 0,
        effective: 0,
      },
    ]);
  });
});

describe("toMixBySource — the card's rows become the API's rows", () => {
  const explicit = (sourceId: string, percent: number) => ({
    sourceId,
    name: sourceId,
    mediaCount: 1,
    state: "active",
    mode: "explicit" as const,
    percent,
    effective: 0,
  });
  const automatic = (sourceId: string) => ({
    sourceId,
    name: sourceId,
    mediaCount: 1,
    state: "active",
    mode: "automatic" as const,
    percent: 0,
    effective: 0,
  });
  const off = (sourceId: string) => ({
    sourceId,
    name: sourceId,
    mediaCount: 1,
    state: "active",
    mode: "off" as const,
    percent: 0,
    effective: 0,
  });

  it("sends explicit rows summing to 100 as ratios, off as 0, and leaves automatic rows out", () => {
    expect(
      toMixBySource([
        explicit(S1, 70),
        explicit(S2, 30),
        automatic(S3),
        off("s4"),
      ]),
    ).toEqual({
      ok: true,
      rows: [
        { source_id: S1, ratio: 0.7 },
        { source_id: S2, ratio: 0.3 },
        { source_id: "s4", ratio: 0 },
      ],
    });
  });
  it("tolerates a thirds split", () => {
    expect(
      toMixBySource([
        explicit(S1, 33.3),
        explicit(S2, 33.3),
        explicit(S3, 33.4),
      ]).ok,
    ).toBe(true);
  });
  it("refuses explicit rows that do not add up to 100", () => {
    expect(
      toMixBySource([explicit(S1, 70), explicit(S2, 20), automatic(S3)]),
    ).toEqual({ ok: false, error: "sum_not_100", total: 90 });
  });
  it("refuses a bad percentage by name", () => {
    expect(toMixBySource([explicit(S1, 120)]).ok).toBe(false);
    expect(toMixBySource([explicit(S1, Number.NaN)]).ok).toBe(false);
  });
  it("every folder automatic is an empty mix", () => {
    expect(toMixBySource([automatic(S1), automatic(S2)])).toEqual({
      ok: true,
      rows: [],
    });
  });
  it("every folder off is refused: something must post", () => {
    expect(toMixBySource([off(S1), off(S2)])).toEqual({
      ok: false,
      error: "all_off",
    });
  });
  it("an explicit row at 0 counts as off, never dropped", () => {
    expect(toMixBySource([explicit(S1, 100), explicit(S2, 0)])).toEqual({
      ok: true,
      rows: [
        { source_id: S1, ratio: 1 },
        { source_id: S2, ratio: 0 },
      ],
    });
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
  it("PUTs rows by source and reads the view back", async () => {
    const captured: { url: string; init?: RequestInit }[] = [];
    const view = {
      rows: [row({ ratio: 1, effective: 100 })],
      explicit_total: 1,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        captured.push({ url, init });
        return new Response(JSON.stringify(view), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    const result = await saveCategoryMix(WS, [{ source_id: S1, ratio: 1 }]);
    expect(result).toEqual({ ok: true, rows: view.rows });
    expect(captured[0].url).toBe(`/api/workspaces/${WS}/category-mix`);
    expect(captured[0].init?.method).toBe("PUT");
    expect(JSON.parse(String(captured[0].init?.body))).toEqual({
      rows: [{ source_id: S1, ratio: 1 }],
    });
  });
});

describe("refusal copy", () => {
  it("names the mix refusals a person can act on", () => {
    expect(mixRefusalCopy("invalid_mix_unknown_source")).toMatch(
      /no longer connected/,
    );
    expect(mixRefusalCopy("invalid_mix_all_off")).toMatch(/at least one/i);
    expect(mixRefusalCopy("invalid_mix_sum_not_one")).toMatch(/100/);
  });
  it("says why a nested folder pick was refused", () => {
    expect(addFolderRefusalCopy("source_nested")).toMatch(/already connected/);
  });
});
