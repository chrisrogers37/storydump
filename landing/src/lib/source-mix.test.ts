import { afterEach, describe, expect, it, vi } from "vitest";
import {
  checkDraft,
  compareDraft,
  conversionNotice,
  countsLine,
  destinationsDiffer,
  editDraft,
  enabledSharesLine,
  largestRemainder,
  newerMix,
  offCopy,
  openDraft,
  parseSourceMix,
  percentText,
  previewSourceMix,
  putSourceMix,
  ratioText,
  readPercent,
  rebaseDraft,
  resetEqualAction,
  saveCustomAction,
  setEnabledAction,
  sharePercents,
  sourceAccessibleName,
  sourceSubtitle,
  sourceTitle,
  targetLabel,
  versionFields,
  type SourceMix,
} from "./source-mix";

/**
 * The protocol-2 mix, read, shown and written back. Every fixture is synthetic:
 * `example_handle` is no one's account.
 */

const WS = "11111111-1111-4111-8111-111111111111";
const IG = "22222222-2222-4222-8222-222222222222";
const DRIVE = "33333333-3333-4333-8333-333333333333";
const DRIVE_2 = "44444444-4444-4444-8444-444444444444";
const GONE = "77777777-7777-4777-8777-777777777777";
const ACCOUNT_A = "55555555-5555-4555-8555-555555555555";
const ACCOUNT_B = "66666666-6666-4666-8666-666666666666";

/** A third as the API writes it: fifty digits, the residual on one of them. */
const THIRD = "0.33333333333333333333333333333333333333333333333333";
const THIRD_AND_RESIDUAL = "0.33333333333333333333333333333333333333333333333334";

function igRow(over: Record<string, unknown> = {}) {
  return {
    source_id: IG,
    provider: "instagram_archive",
    name: "example_handle",
    state: "active",
    origin_account_id: ACCOUNT_A,
    origin_handle: "example_handle",
    enabled: true,
    ratio: "0.5",
    target_share: "0.5",
    media_count: 700,
    indexed: 680,
    ...over,
  };
}

function driveRow(over: Record<string, unknown> = {}) {
  return {
    source_id: DRIVE,
    provider: "gdrive",
    name: "Event photos",
    state: "active",
    origin_account_id: null,
    origin_handle: null,
    enabled: true,
    ratio: "0.5",
    target_share: "0.5",
    media_count: 40,
    indexed: null,
    ...over,
  };
}

function wire(over: Record<string, unknown> = {}) {
  return {
    protocol_version: 2,
    policy_version: 1,
    revision: 8,
    mode: "equal",
    supported_actions: ["reset_equal", "set_enabled", "save_custom"],
    conversion: { state: "complete", kind: "automatic_to_equal" },
    rows: [igRow(), driveRow()],
    destinations: [
      {
        account_id: ACCOUNT_A,
        handle: "example_handle",
        display_name: "Example",
        state: "ready",
        tone: "active",
        candidates: 136,
        eligible: 3,
        sources: [
          { source_id: IG, assignable: true, candidates: 124, eligible: 0, effective_share: "0.5" },
          { source_id: DRIVE, assignable: true, candidates: 12, eligible: 3, effective_share: "0.5" },
        ],
      },
    ],
    guidance: { active: 1, ready: 1, intentional: null, incomplete: [] },
    archive_available: true,
    reset_explanation: null,
    ...over,
  };
}

/** What a destination's entry for a source reads as when the API sent none of the progress facts. */
const NO_PROGRESS = {
  indexing: null,
  partialCoverage: null,
  stalled: null,
  needsReconnect: null,
  oldestObservedAt: null,
};

function mix(over: Record<string, unknown> = {}): SourceMix {
  const parsed = parseSourceMix(wire(over));
  if (!parsed) throw new Error("fixture did not parse");
  return parsed;
}

describe("reading a protocol-2 mix", () => {
  it("reads the contract's rows, destinations and flags", () => {
    const read = mix();
    expect(read.revision).toBe(8);
    expect(read.mode).toBe("equal");
    expect(read.conversionKind).toBe("automatic_to_equal");
    expect(read.archiveAvailable).toBe(true);
    expect(read.supportedActions).toEqual(["reset_equal", "set_enabled", "save_custom"]);
    expect(read.rows[0]).toEqual({
      sourceId: IG,
      provider: "instagram_archive",
      name: "example_handle",
      state: "active",
      enabled: true,
      ratio: "0.5",
      targetShare: "0.5",
      originHandle: "example_handle",
      mediaCount: 700,
      indexed: 680,
    });
    expect(read.rows[1].indexed).toBeNull();
    expect(read.destinations).toEqual([
      {
        accountId: ACCOUNT_A,
        handle: "example_handle",
        displayName: "Example",
        sources: [
          { sourceId: IG, assignable: true, candidates: 124, effectiveShare: "0.5", ...NO_PROGRESS },
          { sourceId: DRIVE, assignable: true, candidates: 12, effectiveShare: "0.5", ...NO_PROGRESS },
        ],
      },
    ]);
  });

  it("reads an archive's progress for each destination as the API sent it, and nothing it did not", () => {
    const read = mix({
      destinations: [
        {
          account_id: ACCOUNT_A,
          handle: "example_handle",
          sources: [
            {
              source_id: IG,
              assignable: true,
              candidates: 124,
              effective_share: "0.5",
              indexing: true,
              partial_coverage: true,
              stalled: false,
              needs_reconnect: false,
              oldest_observed_at: "2020-03-15T10:00:00+00:00",
            },
            // A Drive folder has none of these, and the API sends them false and null.
            {
              source_id: DRIVE,
              assignable: true,
              candidates: 12,
              effective_share: "0.5",
              indexing: false,
              partial_coverage: false,
              stalled: false,
              needs_reconnect: false,
              oldest_observed_at: null,
            },
          ],
        },
      ],
    });
    const [ig, drive] = read.destinations[0].sources;
    expect(ig).toMatchObject({
      indexing: true,
      partialCoverage: true,
      stalled: false,
      needsReconnect: false,
      oldestObservedAt: "2020-03-15T10:00:00+00:00",
    });
    expect(drive).toMatchObject({
      indexing: false,
      partialCoverage: false,
      stalled: false,
      needsReconnect: false,
      oldestObservedAt: null,
    });
  });

  it("leaves a progress fact the API did not send, or sent as another type, unknown rather than false", () => {
    const read = mix({
      destinations: [
        {
          account_id: ACCOUNT_A,
          handle: "example_handle",
          sources: [
            {
              source_id: IG,
              indexing: "true",
              partial_coverage: 1,
              stalled: null,
              oldest_observed_at: "not a time",
            },
          ],
        },
      ],
    });
    expect(read.destinations[0].sources[0]).toEqual({
      sourceId: IG,
      assignable: null,
      candidates: null,
      effectiveShare: null,
      ...NO_PROGRESS,
    });
  });

  it("keeps a fifty-digit share as the string it was", () => {
    const read = mix({
      rows: [
        igRow({ ratio: THIRD_AND_RESIDUAL, target_share: THIRD_AND_RESIDUAL }),
        driveRow({ ratio: THIRD, target_share: THIRD }),
        driveRow({ source_id: DRIVE_2, ratio: THIRD, target_share: THIRD }),
      ],
    });
    expect(read.rows.map((r) => r.targetShare)).toEqual([THIRD_AND_RESIDUAL, THIRD, THIRD]);
  });

  it("is not protocol 2 when the read says another protocol", () => {
    expect(parseSourceMix({ ...wire(), protocol_version: 1 })).toBeNull();
    expect(parseSourceMix({ rows: [] })).toBeNull();
    expect(parseSourceMix(null)).toBeNull();
  });

  it("refuses an answer a write could not be built from", () => {
    // A float ratio is not the API's decimal string: reading it would round it.
    expect(parseSourceMix(wire({ rows: [igRow({ ratio: 0.5 })] }))).toBeNull();
    expect(parseSourceMix(wire({ rows: [igRow({ source_id: undefined })] }))).toBeNull();
    expect(parseSourceMix(wire({ rows: [igRow({ enabled: "yes" })] }))).toBeNull();
    expect(parseSourceMix(wire({ revision: "8" }))).toBeNull();
    expect(parseSourceMix(wire({ mode: "automatic" }))).toBeNull();
    expect(parseSourceMix(wire({ rows: "none" }))).toBeNull();
  });

  it("draws what an API sends without the optional facts, leaving them unknown", () => {
    const read = parseSourceMix({
      protocol_version: 2,
      revision: 3,
      mode: "custom",
      rows: [{ source_id: DRIVE, provider: "gdrive", enabled: true, ratio: "1", target_share: "1" }],
    });
    expect(read).not.toBeNull();
    expect(read!.rows[0].mediaCount).toBeNull();
    expect(read!.rows[0].indexed).toBeNull();
    expect(read!.destinations).toEqual([]);
    expect(read!.archiveAvailable).toBe(false);
    expect(read!.supportedActions).toEqual([]);
    expect(read!.conversionKind).toBeNull();
  });

  it("takes any @ off a handle, so the card adds exactly one", () => {
    expect(mix({ rows: [igRow({ origin_handle: "@example_handle" })] }).rows[0].originHandle).toBe(
      "example_handle",
    );
  });
});

describe("the newer reading wins", () => {
  it("is the higher revision, and the page's read on a tie", () => {
    const page = mix({ revision: 9 });
    const answer = mix({ revision: 10 });
    expect(newerMix(page, answer)).toBe(answer);
    expect(newerMix(answer, page)).toBe(answer);
    const same = mix({ revision: 9 });
    expect(newerMix(page, same)).toBe(page);
    expect(newerMix(null, same)).toBe(same);
    expect(newerMix(page, null)).toBe(page);
  });
});

describe("shares shown as percents that add to 100", () => {
  it("shows one source as 100%", () => {
    expect(sharePercents(["1"])).toEqual([100]);
  });

  it("shows two as 50 and 50", () => {
    expect(sharePercents(["0.5", "0.5"])).toEqual([50, 50]);
  });

  it("shows thirds as 33.4, 33.3 and 33.3, the extra tenth on the residual's row", () => {
    expect(sharePercents([THIRD, THIRD_AND_RESIDUAL, THIRD])).toEqual([33.3, 33.4, 33.3]);
    // Equal digits tie, and a tie goes to the earlier row.
    expect(sharePercents([THIRD, THIRD, THIRD])).toEqual([33.4, 33.3, 33.3]);
    expect(sharePercents(["0.25", "0.25", "0.25", "0.25"])).toEqual([25, 25, 25, 25]);
  });

  it("shows every source Off as 0s: X = 0 has nothing to share out", () => {
    expect(sharePercents(["0", "0"])).toEqual([0, 0]);
    expect(sharePercents([])).toEqual([]);
  });

  it("shows an Off row as 0 beside the shares of the rows that are on", () => {
    expect(sharePercents(["0", "0.5", "0.5"])).toEqual([0, 50, 50]);
  });

  it("shows a skewed custom mix exactly", () => {
    expect(sharePercents(["0.999", "0.001"])).toEqual([99.9, 0.1]);
    expect(sharePercents(["0.875", "0.125"])).toEqual([87.5, 12.5]);
  });

  it("compares the remainders' digits, not floats that cannot tell them apart", () => {
    // In floating point these two thirds are the same number.
    expect(Number(THIRD)).toBe(Number(THIRD_AND_RESIDUAL));
    expect(largestRemainder([THIRD, THIRD_AND_RESIDUAL], 4)).toEqual([3333, 3334]);
  });

  it("never shows a source that is on as 0%, however small its share", () => {
    expect(sharePercents(["0.99999", "0.00001"])).toEqual([100, 0]);
    expect(targetLabel({ enabled: true }, 0)).toBe("<0.1%");
    expect(targetLabel({ enabled: false }, 0)).toBe("Off");
    expect(targetLabel({ enabled: true }, 33.3)).toBe("33.3%");
  });
});

describe("what a row says", () => {
  const read = mix();
  const [ig, drive] = read.rows;
  const [account] = read.destinations;

  it("titles Instagram as Your Instagram with its handle, and Drive by its folder", () => {
    expect(sourceTitle(ig)).toBe("Your Instagram");
    expect(sourceSubtitle(ig)).toBe("@example_handle · Instagram");
    expect(sourceAccessibleName(ig)).toBe("Your Instagram @example_handle");
    expect(sourceTitle(drive)).toBe("Event photos");
    expect(sourceSubtitle(drive)).toBe("Google Drive");
    expect(sourceAccessibleName(drive)).toBe("Event photos (Google Drive)");
  });

  it("counts matching posts for the viewed account, then indexed or files", () => {
    expect(countsLine(ig, account)).toBe("124 matching items · 680 posts indexed");
    expect(countsLine(drive, account)).toBe("12 matching posts · 40 files");
  });

  it("counts one in the singular", () => {
    const single = mix({
      rows: [driveRow({ media_count: 1 })],
      destinations: [
        { account_id: ACCOUNT_A, handle: "example_handle", sources: [{ source_id: DRIVE, candidates: 1, effective_share: "1" }] },
      ],
    });
    expect(countsLine(single.rows[0], single.destinations[0])).toBe("1 matching post · 1 file");
  });

  it("names no matching posts without an account to view, and no indexed count without evidence", () => {
    expect(countsLine(ig, null)).toBe("680 posts indexed");
    const unindexed = mix({ rows: [igRow({ indexed: null })] });
    expect(countsLine(unindexed.rows[0], account)).toBe("124 matching items");
  });

  it("says a source cannot serve the viewed account, instead of counting it", () => {
    const other = mix({
      destinations: [
        {
          account_id: ACCOUNT_B,
          handle: "other_handle",
          sources: [
            { source_id: IG, assignable: false, candidates: 0, effective_share: "0" },
            { source_id: DRIVE, assignable: true, candidates: 12, effective_share: "1" },
          ],
        },
      ],
    });
    expect(countsLine(other.rows[0], other.destinations[0])).toBe("Not used for @other_handle");
  });

  it("says what Off does, per provider, and never that it deletes", () => {
    expect(offCopy("instagram_archive")).toBe(
      "Stops indexing and selecting past posts; indexed metadata stays until deletion.",
    );
    expect(offCopy("gdrive")).toBe("Stops selecting this folder; syncing continues.");
    for (const provider of ["instagram_archive", "gdrive", "other"]) {
      expect(offCopy(provider).toLowerCase()).not.toContain("delete history");
    }
  });

  it("names the conversion that happened, and nothing for a mix born on protocol 2", () => {
    expect(conversionNotice("automatic_to_equal", "equal")).toMatch(/became Equal shares/);
    expect(conversionNotice("explicit_preserved", "custom")).toMatch(/kept as you set them/);
    const hybrid = conversionNotice("hybrid_to_custom", "custom")!;
    expect(hybrid).toMatch(/translated once into fixed custom percentages/);
    expect(hybrid).toMatch(/how many items each source held/);
    // It claims no exact match with what Automatic drew.
    expect(hybrid.toLowerCase()).not.toMatch(/identical|exactly|same ratios/);
    for (const mode of ["equal", "custom"] as const) {
      expect(conversionNotice("new_equal", mode)).toBeNull();
      expect(conversionNotice("legacy_intent", mode)).toBeNull();
      expect(conversionNotice(null, mode)).toBeNull();
    }
  });

  it("says a conversion only while the mix is still the mode it left it in", () => {
    // Automatic became Equal: said while Equal, gone once the mix is Custom.
    expect(conversionNotice("automatic_to_equal", "equal")).not.toBeNull();
    expect(conversionNotice("automatic_to_equal", "custom")).toBeNull();
    // A custom mix kept, or translated from a hybrid: said while Custom, gone once Equal.
    for (const kind of ["explicit_preserved", "hybrid_to_custom"]) {
      expect(conversionNotice(kind, "custom"), kind).not.toBeNull();
      expect(conversionNotice(kind, "equal"), kind).toBeNull();
    }
  });
});

describe("whether the accounts are told apart", () => {
  const account = (id: string, sources: Array<[string, boolean | undefined, string?]>) => ({
    account_id: id,
    handle: id,
    sources: sources.map(([source_id, assignable, effective_share]) => ({
      source_id,
      assignable,
      ...(effective_share === undefined ? {} : { effective_share }),
    })),
  });
  const destinations = (...entries: ReturnType<typeof account>[]) => mix({ destinations: entries }).destinations;

  it("is not asked of no account or of one", () => {
    expect(destinationsDiffer([])).toBe(false);
    expect(destinationsDiffer(destinations(account(ACCOUNT_A, [[IG, true], [DRIVE, true]])))).toBe(false);
  });

  it("is false for accounts served by the same sources, which have one mix between them", () => {
    // A workspace of Drive folders alone: a folder serves every account.
    expect(
      destinationsDiffer(
        destinations(account(ACCOUNT_A, [[DRIVE, true], [DRIVE_2, true]]), account(ACCOUNT_B, [[DRIVE, true], [DRIVE_2, true]])),
      ),
    ).toBe(false);
    // However the entries are ordered.
    expect(
      destinationsDiffer(
        destinations(account(ACCOUNT_A, [[DRIVE, true], [DRIVE_2, true]]), account(ACCOUNT_B, [[DRIVE_2, true], [DRIVE, true]])),
      ),
    ).toBe(false);
  });

  it("is true where an archive serves one account and not the other", () => {
    expect(
      destinationsDiffer(
        destinations(account(ACCOUNT_A, [[IG, true], [DRIVE, true]]), account(ACCOUNT_B, [[IG, false], [DRIVE, true]])),
      ),
    ).toBe(true);
  });

  it("is true when one account is served by a source another has no entry for", () => {
    expect(
      destinationsDiffer(destinations(account(ACCOUNT_A, [[DRIVE, true], [DRIVE_2, true]]), account(ACCOUNT_B, [[DRIVE, true]]))),
    ).toBe(true);
  });

  it("is true if any two of three differ", () => {
    expect(
      destinationsDiffer(
        destinations(
          account(ACCOUNT_A, [[DRIVE, true]]),
          account(ACCOUNT_B, [[DRIVE, true]]),
          account("77777777-7777-4777-8777-777777777777", [[IG, true], [DRIVE, true]]),
        ),
      ),
    ).toBe(true);
  });

  it("is true for accounts with the same scope whose effective shares differ", () => {
    // Shares renormalize over the posts each account can use: a folder with none
    // for B weighs 0 there, so A and B draw differently from the same two folders.
    expect(
      destinationsDiffer(
        destinations(
          account(ACCOUNT_A, [[DRIVE, true, "0.5"], [DRIVE_2, true, "0.5"]]),
          account(ACCOUNT_B, [[DRIVE, true, "0"], [DRIVE_2, true, "1"]]),
        ),
      ),
    ).toBe(true);
  });

  it("is false for the same scope and the same shares, however the API wrote them", () => {
    expect(
      destinationsDiffer(
        destinations(
          account(ACCOUNT_A, [[DRIVE, true, "0.5"], [DRIVE_2, true, "0.5"]]),
          account(ACCOUNT_B, [[DRIVE, true, "0.50"], [DRIVE_2, true, "0.5000"]]),
        ),
      ),
    ).toBe(false);
    expect(
      destinationsDiffer(
        destinations(account(ACCOUNT_A, [[DRIVE, true, "1"]]), account(ACCOUNT_B, [[DRIVE, true, "1.0"]])),
      ),
    ).toBe(false);
  });

  it("compares shares only where the API sent both", () => {
    // A share one account has and the other has none for says nothing: it is unknown, not different.
    expect(
      destinationsDiffer(
        destinations(account(ACCOUNT_A, [[DRIVE, true, "1"]]), account(ACCOUNT_B, [[DRIVE, true]])),
      ),
    ).toBe(false);
  });

  it("does not count a source the API did not say serves an account", () => {
    // Unknown is not serving, so two accounts the API said nothing about share a scope.
    expect(
      destinationsDiffer(destinations(account(ACCOUNT_A, [[IG, undefined]]), account(ACCOUNT_B, [[IG, undefined]]))),
    ).toBe(false);
    expect(
      destinationsDiffer(destinations(account(ACCOUNT_A, [[IG, true]]), account(ACCOUNT_B, [[IG, undefined]]))),
    ).toBe(true);
  });
});

describe("the sources that are on, said as one line", () => {
  it("names each source that is on with its share, exactly as the mix sends it", () => {
    expect(enabledSharesLine(mix())).toBe(
      "Your Instagram @example_handle 50%, Event photos (Google Drive) 50%",
    );
    const skewed = mix({
      mode: "custom",
      rows: [igRow({ ratio: "0.7", target_share: "0.7" }), driveRow({ ratio: "0.3", target_share: "0.3" })],
    });
    expect(enabledSharesLine(skewed)).toBe(
      "Your Instagram @example_handle 70%, Event photos (Google Drive) 30%",
    );
  });

  it("leaves a source that is Off out, and is null when none is on", () => {
    const oneOff = mix({
      rows: [igRow({ ratio: "1", target_share: "1" }), driveRow({ enabled: false, ratio: "0", target_share: "0" })],
    });
    expect(enabledSharesLine(oneOff)).toBe("Your Instagram @example_handle 100%");
    const allOff = mix({
      rows: [
        igRow({ enabled: false, ratio: "0", target_share: "0" }),
        driveRow({ enabled: false, ratio: "0", target_share: "0" }),
      ],
    });
    expect(enabledSharesLine(allOff)).toBeNull();
  });
});

describe("the fields a write to the sources carries", () => {
  it("are the protocol and the revision on a converted workspace, the revision 0 included", () => {
    expect(versionFields(8)).toEqual({ protocol_version: 2, expected_revision: 8 });
    expect(versionFields(0)).toEqual({ protocol_version: 2, expected_revision: 0 });
  });

  it("are nothing at all on protocol 1, so its writes carry neither", () => {
    expect(versionFields(null)).toEqual({});
    expect(Object.keys(versionFields(null))).toEqual([]);
  });
});

describe("typed percentages and the ratios they save", () => {
  it("reads up to two decimals into basis points, exactly", () => {
    expect(readPercent("33.33")).toBe(3333);
    expect(readPercent("100")).toBe(10000);
    expect(readPercent("12.5")).toBe(1250);
    expect(readPercent(" 0.5 ")).toBe(50);
    expect(readPercent(".5")).toBe(50);
    expect(readPercent("7.")).toBe(700);
  });

  it("refuses what is not a percentage with at most two decimals", () => {
    for (const typed of ["", ".", "abc", "1,5", "-1", "1e2", "33.333", "1000"]) {
      expect(readPercent(typed), typed).toBeNull();
    }
  });

  it("writes basis points as the decimal strings the API stores, with no float between", () => {
    expect(ratioText(3333)).toBe("0.3333");
    expect(ratioText(5000)).toBe("0.5");
    expect(ratioText(10000)).toBe("1");
    expect(ratioText(1)).toBe("0.0001");
    expect(ratioText(0)).toBe("0");
    // 10% + 20% + 70%: 0.1 + 0.2 is 0.30000000000000004 as floats; as basis points it is not.
    expect([1000, 2000, 7000].map(ratioText)).toEqual(["0.1", "0.2", "0.7"]);
  });

  it("shows basis points the way a person types them", () => {
    expect(percentText(3333)).toBe("33.33");
    expect(percentText(1250)).toBe("12.5");
    expect(percentText(5000)).toBe("50");
    expect(percentText(1)).toBe("0.01");
  });
});

describe("a custom draft", () => {
  const thirds = mix({
    rows: [
      igRow({ ratio: THIRD, target_share: THIRD }),
      driveRow({ ratio: THIRD_AND_RESIDUAL, target_share: THIRD_AND_RESIDUAL }),
      driveRow({ source_id: DRIVE_2, name: "Old", enabled: false, ratio: "0", target_share: "0" }),
      driveRow({ source_id: GONE, name: "Events", ratio: THIRD, target_share: THIRD }),
    ],
  });

  it("opens at the target shares, adding to exactly 100, with Off rows at 0", () => {
    const draft = openDraft(thirds);
    expect(draft.basedOn).toBe(8);
    expect(draft.rows.map((r) => r.percent)).toEqual(["33.33", "33.34", "0", "33.33"]);
  });

  it("never opens a source that is on at 0", () => {
    const tiny = mix({
      rows: [igRow({ ratio: "0.99999", target_share: "0.99999" }), driveRow({ ratio: "0.00001", target_share: "0.00001" })],
    });
    expect(openDraft(tiny).rows.map((r) => r.percent)).toEqual(["99.99", "0.01"]);
  });

  it("edits a source that is on, and leaves an Off row at 0", () => {
    let draft = openDraft(thirds);
    draft = editDraft(draft, IG, "50");
    draft = editDraft(draft, DRIVE_2, "40");
    expect(draft.rows.map((r) => r.percent)).toEqual(["50", "33.34", "0", "33.33"]);
  });

  it("saves every source, Off as 0, when the percentages add to 100", () => {
    let draft = openDraft(thirds);
    draft = editDraft(draft, IG, "50");
    draft = editDraft(draft, DRIVE, "30");
    draft = editDraft(draft, GONE, "20");
    expect(checkDraft(draft)).toEqual({
      ok: true,
      totalBp: 10000,
      rows: [
        { source_id: IG, ratio: "0.5" },
        { source_id: DRIVE, ratio: "0.3" },
        { source_id: DRIVE_2, ratio: "0" },
        { source_id: GONE, ratio: "0.2" },
      ],
    });
  });

  it("takes a skewed mix exactly", () => {
    let draft = openDraft(mix());
    draft = editDraft(draft, IG, "99.99");
    draft = editDraft(draft, DRIVE, "0.01");
    const check = checkDraft(draft);
    expect(check.ok && check.rows.map((r) => r.ratio)).toEqual(["0.9999", "0.0001"]);
  });

  it("takes 33.33 three times, within the API's tolerance", () => {
    let draft = openDraft(thirds);
    draft = editDraft(draft, DRIVE, "33.33");
    const check = checkDraft(draft);
    expect(check).toMatchObject({ ok: true, totalBp: 9999 });
  });

  it("refuses a total off 100 by more than the tolerance, naming the total", () => {
    let draft = openDraft(mix());
    draft = editDraft(draft, IG, "50");
    draft = editDraft(draft, DRIVE, "49.8");
    expect(checkDraft(draft)).toEqual({ ok: false, error: "sum_not_100", totalBp: 9980 });
    draft = editDraft(draft, DRIVE, "49.9");
    expect(checkDraft(draft).ok).toBe(true);
  });

  it("refuses 0 for a source that is on: that is the switch's change", () => {
    let draft = openDraft(mix());
    draft = editDraft(draft, IG, "0");
    draft = editDraft(draft, DRIVE, "100");
    expect(checkDraft(draft)).toEqual({ ok: false, error: "zero_enabled", sourceId: IG, totalBp: null });
  });

  it("refuses what is not a percentage, and more than 100", () => {
    let draft = editDraft(openDraft(mix()), DRIVE, "half");
    expect(checkDraft(draft)).toMatchObject({ ok: false, error: "bad_percent", sourceId: DRIVE });
    draft = editDraft(openDraft(mix()), IG, "100.01");
    expect(checkDraft(draft)).toMatchObject({ ok: false, error: "bad_percent", sourceId: IG });
  });

  it("has nothing to add up when every source is Off", () => {
    const off = mix({
      rows: [igRow({ enabled: false, ratio: "0", target_share: "0" }), driveRow({ enabled: false, ratio: "0", target_share: "0" })],
    });
    expect(checkDraft(openDraft(off))).toEqual({
      ok: true,
      totalBp: 0,
      rows: [
        { source_id: IG, ratio: "0" },
        { source_id: DRIVE, ratio: "0" },
      ],
    });
  });
});

describe("a kept draft beside the mix as it is now", () => {
  // The draft was opened at revision 8: Instagram 70, Event photos 30,
  // Old Off, Events 0 because it was Off too.
  const before = mix({
    mode: "custom",
    rows: [
      igRow({ ratio: "0.7", target_share: "0.7" }),
      driveRow({ ratio: "0.3", target_share: "0.3" }),
      driveRow({ source_id: DRIVE_2, name: "Old", enabled: false, ratio: "0", target_share: "0" }),
      driveRow({ source_id: GONE, name: "Events", enabled: false, ratio: "0", target_share: "0" }),
    ],
  });
  let draft = openDraft(before);
  draft = editDraft(draft, IG, "60");
  draft = editDraft(draft, DRIVE, "40");
  // Meanwhile, revision 10: Event photos was turned off, Old turned on, Events
  // removed, and a new folder added; the enabled set reset to equal.
  const NEW = "88888888-8888-4888-8888-888888888888";
  const now = mix({
    revision: 10,
    rows: [
      igRow({ ratio: THIRD_AND_RESIDUAL, target_share: THIRD_AND_RESIDUAL }),
      driveRow({ enabled: false, ratio: "0", target_share: "0" }),
      driveRow({ source_id: DRIVE_2, name: "Old", enabled: true, ratio: THIRD, target_share: THIRD }),
      driveRow({ source_id: NEW, name: "New folder", enabled: true, ratio: THIRD, target_share: THIRD }),
    ],
  });

  it("marks every row by what changed, and lists the removed row last", () => {
    expect(
      compareDraft(draft, now).map((r) => [r.row.sourceId, r.change, r.draftPercent, r.draftEnabled]),
    ).toEqual([
      [IG, "kept", "60", true],
      [DRIVE, "turned_off", "40", true],
      [DRIVE_2, "turned_on", "0", false],
      [NEW, "added", null, null],
      [GONE, "removed", "0", false],
    ]);
  });

  it("keeps the saved share now beside each, and none for the removed row", () => {
    expect(compareDraft(draft, now).map((r) => r.savedShare)).toEqual([
      THIRD_AND_RESIDUAL,
      "0",
      THIRD,
      THIRD,
      null,
    ]);
  });

  it("carries the draft over only by choice: typed values where still on, current targets for the rest", () => {
    const carried = rebaseDraft(draft, now);
    expect(carried.basedOn).toBe(10);
    expect(carried.rows.map((r) => [r.row.sourceId, r.percent])).toEqual([
      [IG, "60"],
      [DRIVE, "0"],
      [DRIVE_2, "33.33"],
      [NEW, "33.33"],
    ]);
    // It is a draft like any other: it saves only when it adds up.
    expect(checkDraft(carried)).toEqual({ ok: false, error: "sum_not_100", totalBp: 12666 });
  });

  it("leaves the kept draft itself untouched", () => {
    rebaseDraft(draft, now);
    expect(draft.basedOn).toBe(8);
    expect(draft.rows.map((r) => r.percent)).toEqual(["60", "40", "0", "0"]);
  });
});

describe("the action bodies", () => {
  it("are the contract's, with the protocol and the revision", () => {
    expect(resetEqualAction(8)).toEqual({ protocol_version: 2, action: "reset_equal", expected_revision: 8 });
    expect(setEnabledAction(8, IG, false)).toEqual({
      protocol_version: 2,
      action: "set_enabled",
      source_id: IG,
      enabled: false,
      expected_revision: 8,
    });
    expect(saveCustomAction(8, [{ source_id: IG, ratio: "1" }])).toEqual({
      protocol_version: 2,
      action: "save_custom",
      expected_revision: 8,
      rows: [{ source_id: IG, ratio: "1" }],
    });
  });
});

describe("the two doors", () => {
  afterEach(() => vi.unstubAllGlobals());

  function stub(status: number, body: unknown) {
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url, init });
        return new Response(JSON.stringify(body), {
          status,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    return calls;
  }

  it("PUTs the action unchanged and reads the whole mix back", async () => {
    const calls = stub(200, wire({ revision: 9, reset_explanation: "enabled_membership_changed" }));
    const action = setEnabledAction(8, IG, false);
    const result = await putSourceMix(WS, action);
    expect(calls[0].url).toBe(`/api/workspaces/${WS}/category-mix`);
    expect(calls[0].init?.method).toBe("PUT");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual(action);
    expect(result.ok && result.mix.revision).toBe(9);
    expect(result.ok && result.mix.resetExplanation).toBe("enabled_membership_changed");
  });

  it("POSTs a preview to its own route", async () => {
    const calls = stub(200, wire());
    const action = resetEqualAction(8);
    const result = await previewSourceMix(WS, action);
    expect(calls[0].url).toBe(`/api/workspaces/${WS}/category-mix/preview`);
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual(action);
    expect(result.ok).toBe(true);
  });

  it("keeps a conflict's revision", async () => {
    stub(409, { error: "mix_conflict", current_revision: 9 });
    expect(await putSourceMix(WS, resetEqualAction(8))).toEqual({
      ok: false,
      error: "mix_conflict",
      status: 409,
      currentRevision: 9,
    });
  });

  it("says a 2xx it cannot read is malformed, not saved", async () => {
    stub(200, { rows: [] });
    expect(await putSourceMix(WS, resetEqualAction(8))).toEqual({
      ok: false,
      error: "malformed_response",
      status: 200,
      currentRevision: null,
    });
  });
});
