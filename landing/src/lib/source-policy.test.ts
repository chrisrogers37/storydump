import { afterEach, describe, expect, it, vi } from "vitest";
import {
  INITIAL_POLICY_FLOW,
  ageBoundsCopy,
  ageCheckCopy,
  ageFactsCopy,
  checkPolicyDraft,
  fetchSourcePolicy,
  parseSourcePolicy,
  policyDraft,
  policyDraftChanged,
  policyDraftStale,
  policyFlowReducer,
  saveSourcePolicy,
  type PolicyFlow,
  type PolicyFlowEvent,
  type SourcePolicy,
} from "./source-policy";
import { SOURCE_REFUSAL_COPY } from "./source-refusals";

/**
 * An Instagram source's policy, as the API's policy resource sends it:
 * the editor draws what the server says is editable, within the server's
 * bounds, and a refusal never drops what was typed.
 */

const WS = "11111111-1111-4111-8111-111111111111";
const SOURCE = "22222222-2222-4222-8222-222222222222";

function envelope(over: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    revision: 4,
    policy: {
      minimum_age_days: 90,
      include_credited: false,
      include_unknown: false,
      eligibility: "supported",
      ranking: "none",
      destination_scope: "origin_only",
    },
    effective: { minimum_age_days: 90 },
    bounds: { minimum_age_days: { min: 0, max: 36500 } },
    capabilities: { editable: ["minimum_age_days", "include_credited", "include_unknown"] },
    ...over,
  };
}

function policy(over: Record<string, unknown> = {}): SourcePolicy {
  const parsed = parseSourcePolicy(envelope(over));
  if (!parsed) throw new Error("fixture did not parse");
  return parsed;
}

describe("reading the policy envelope", () => {
  it("reads version 2 carousel choices and sends the selected value", () => {
    const saved = policy({
      schema_version: 2,
      policy: { minimum_age_days: 90, include_credited: false, include_unknown: false, carousel_slides: "all" },
      choices: { carousel_slides: ["all", "first"] },
      capabilities: { editable: ["minimum_age_days", "include_credited", "include_unknown", "carousel_slides"] },
    });
    expect(saved.carouselChoices).toEqual(["all", "first"]);
    expect(saved.editable).toContain("carousel_slides");
    expect(checkPolicyDraft({ ...policyDraft(saved), carouselSlides: "first" }, saved)).toMatchObject({
      ok: true, body: { schema_version: 2, carousel_slides: "first" },
    });
    expect(checkPolicyDraft({ ...policyDraft(saved), carouselSlides: "all" }, saved).ok).toBe(true);
  });

  it("reads the saved values, the age in effect, the bounds and what is editable", () => {
    expect(policy({ effective: { minimum_age_days: 121 } })).toEqual({
      schemaVersion: 1,
      revision: 4,
      minimumAgeDays: 90,
      includeCredited: false,
      includeUnknown: false,
      carouselSlides: null,
      carouselChoices: [],
      effectiveMinimumAgeDays: 121,
      ageBounds: { min: 0, max: 36500 },
      editable: ["minimum_age_days", "include_credited", "include_unknown"],
    });
  });

  it("draws no field the server did not list, and none it has no words for", () => {
    const read = policy({ capabilities: { editable: ["include_unknown", "ranking", "percentile"] } });
    expect(read.editable).toEqual(["include_unknown"]);
  });

  it("is unreadable without the saved values, or with a decimal age", () => {
    expect(parseSourcePolicy({ ...envelope(), policy: undefined })).toBeNull();
    expect(
      parseSourcePolicy(envelope({ policy: { minimum_age_days: 90.5, include_credited: false, include_unknown: false } })),
    ).toBeNull();
    expect(parseSourcePolicy(envelope({ revision: "4" }))).toBeNull();
  });

  it("leaves the bounds unknown rather than inventing them", () => {
    expect(policy({ bounds: {} }).ageBounds).toBeNull();
    expect(policy({ bounds: { minimum_age_days: { min: 10, max: 5 } } }).ageBounds).toBeNull();
  });
});

describe("what a save sends", () => {
  it("sends the schema, the revision read and the three values", () => {
    const saved = policy();
    const draft = { ...policyDraft(saved), minimumAgeDays: "30", includeUnknown: true };
    expect(checkPolicyDraft(draft, saved)).toEqual({
      ok: true,
      body: {
        schema_version: 1,
        expected_revision: 4,
        minimum_age_days: 30,
        include_credited: false,
        include_unknown: true,
      },
    });
  });

  it("refuses a decimal or a negative age before it is sent", () => {
    const saved = policy();
    for (const typed of ["90.5", "-1", "ninety", ""]) {
      expect(checkPolicyDraft({ ...policyDraft(saved), minimumAgeDays: typed }, saved), typed).toEqual({
        ok: false,
        error: "age_not_whole",
      });
    }
  });

  it("holds the age to the server's bounds, whatever they are", () => {
    const saved = policy();
    expect(checkPolicyDraft({ ...policyDraft(saved), minimumAgeDays: "36501" }, saved)).toEqual({
      ok: false,
      error: "age_out_of_bounds",
    });
    expect(checkPolicyDraft({ ...policyDraft(saved), minimumAgeDays: "36500" }, saved).ok).toBe(true);
    expect(checkPolicyDraft({ ...policyDraft(saved), minimumAgeDays: "0" }, saved).ok).toBe(true);
    // Other bounds from the server are the ones used: nothing here restates 0 to 36,500.
    const narrow = policy({ bounds: { minimum_age_days: { min: 7, max: 30 } } });
    expect(checkPolicyDraft({ ...policyDraft(narrow), minimumAgeDays: "6" }, narrow).ok).toBe(false);
    expect(checkPolicyDraft({ ...policyDraft(narrow), minimumAgeDays: "7" }, narrow).ok).toBe(true);
    expect(checkPolicyDraft({ ...policyDraft(narrow), minimumAgeDays: "31" }, narrow).ok).toBe(false);
  });

  it("sends a field it may not edit as it is saved", () => {
    const saved = policy({ capabilities: { editable: ["minimum_age_days"] } });
    const draft = { ...policyDraft(saved), minimumAgeDays: "120", includeCredited: true };
    const check = checkPolicyDraft(draft, saved);
    expect(check.ok && check.body.include_credited).toBe(false);
    expect(check.ok && check.body.minimum_age_days).toBe(120);
  });

  it("knows an unchanged draft from a changed one", () => {
    const saved = policy();
    expect(policyDraftChanged(policyDraft(saved), saved)).toBe(false);
    expect(policyDraftChanged({ ...policyDraft(saved), includeCredited: true }, saved)).toBe(true);
    expect(policyDraftChanged({ ...policyDraft(saved), minimumAgeDays: " 90 " }, saved)).toBe(false);
  });
});

describe("the age's words", () => {
  it("says the saved and effective age, and why the repost window raised it", () => {
    expect(ageFactsCopy(policy({ effective: { minimum_age_days: 121 } }))).toBe(
      "Saved: 90 days. In effect: 121 days, because the workspace's repost window is longer.",
    );
    expect(ageFactsCopy(policy())).toBe("Saved: 90 days. In effect: 90 days.");
    expect(ageFactsCopy(policy({ effective: {} }))).toBe("Saved: 90 days.");
  });

  it("states the server's bounds", () => {
    expect(ageBoundsCopy({ min: 0, max: 36500 })).toBe("Any whole number of days from 0 to 36,500.");
    expect(ageCheckCopy({ min: 0, max: 36500 })).toBe("Type a whole number of days from 0 to 36,500.");
    expect(ageBoundsCopy(null)).toBeNull();
    expect(ageCheckCopy(null)).toBe("Type a whole number of days.");
  });
});

describe("the editor's states", () => {
  const run = (events: PolicyFlowEvent[], from: PolicyFlow = INITIAL_POLICY_FLOW) =>
    events.reduce(policyFlowReducer, from);
  const loaded = run([{ type: "load_started" }, { type: "loaded", result: { ok: true, policy: policy() } }]);
  const edited = run([{ type: "edited", patch: { minimumAgeDays: "60", includeCredited: true } }], loaded);

  it("opens a draft on the saved values when first read", () => {
    expect(loaded.status).toBe("loaded");
    expect(loaded.draft).toEqual({ basedOn: 4, minimumAgeDays: "90", includeCredited: false, includeUnknown: false, carouselSlides: null });
  });

  it("keeps the draft through a conflict, then shows the saved values beside it", () => {
    const refused = run(
      [
        { type: "save_started" },
        { type: "save_answered", result: { ok: false, error: "policy_conflict", status: 409, currentRevision: 5 } },
      ],
      edited,
    );
    expect(refused.draft).toBe(edited.draft);
    expect(refused.conflict).toBe(true);
    expect(refused.error).toBe(SOURCE_REFUSAL_COPY.policy_conflict);
    // The saved values, read again, land beside the draft without replacing it.
    const fresh = policy({ revision: 5, policy: { minimum_age_days: 30, include_credited: false, include_unknown: true } });
    const reread = run([{ type: "load_started" }, { type: "loaded", result: { ok: true, policy: fresh } }], refused);
    expect(reread.saved).toBe(fresh);
    expect(reread.draft).toEqual({ basedOn: 4, minimumAgeDays: "60", includeCredited: true, includeUnknown: false, carouselSlides: null });
    expect(policyDraftStale(reread)).toBe(true);
    expect(reread.error).toBe(SOURCE_REFUSAL_COPY.policy_conflict);
    // Carried over by choice: the same values, now against revision 5.
    const carried = policyFlowReducer(reread, { type: "rebased" });
    expect(carried.draft).toEqual({ basedOn: 5, minimumAgeDays: "60", includeCredited: true, includeUnknown: false, carouselSlides: null });
    expect(carried.conflict).toBe(false);
    expect(policyDraftStale(carried)).toBe(false);
    expect(checkPolicyDraft(carried.draft!, carried.saved!)).toMatchObject({
      ok: true,
      body: { expected_revision: 5, minimum_age_days: 60 },
    });
    // Or dropped by choice: the saved values again.
    expect(policyFlowReducer(reread, { type: "discarded" }).draft).toEqual(policyDraft(fresh));
  });

  it("names the bounds when the server refuses the age, and keeps the draft", () => {
    const refused = run(
      [
        { type: "save_started" },
        { type: "save_answered", result: { ok: false, error: "invalid_source_policy", status: 400, currentRevision: null } },
      ],
      edited,
    );
    expect(refused.draft).toBe(edited.draft);
    expect(refused.error).toBe(
      "Those settings were refused: the minimum age must be a whole number of days from 0 to 36,500. Nothing changed.",
    );
  });

  it("says a member's save is an admin's, and an unsupported policy is not this page's to change", () => {
    const asMember = run(
      [{ type: "save_started" }, { type: "save_answered", result: { ok: false, error: "http_403", status: 403, currentRevision: null } }],
      edited,
    );
    expect(asMember.error).toBe("You need to be an admin of this workspace to change this. Nothing changed.");
    const unsupported = run(
      [
        { type: "save_started" },
        { type: "save_answered", result: { ok: false, error: "unsupported_source_policy", status: 400, currentRevision: null } },
      ],
      edited,
    );
    expect(unsupported.error).toBe(SOURCE_REFUSAL_COPY.unsupported_source_policy);
    expect(unsupported.draft).toBe(edited.draft);
  });

  it("saves, and starts again from what the server answered", () => {
    const answer = policy({ revision: 5, policy: { minimum_age_days: 60, include_credited: true, include_unknown: false } });
    const done = run([{ type: "save_started" }, { type: "save_answered", result: { ok: true, policy: answer } }], edited);
    expect(done.saved).toBe(answer);
    expect(done.draft).toEqual(policyDraft(answer));
    expect(done.announcement).toBe("Past posts settings saved. They apply from the next selection.");
  });

  it("fails a first read with the reason, and keeps saved values through a failed re-read", () => {
    const failed = run([
      { type: "load_started" },
      { type: "loaded", result: { ok: false, error: "archive_not_activated", status: 503, currentRevision: null } },
    ]);
    expect(failed.status).toBe("failed");
    expect(failed.error).toBe(SOURCE_REFUSAL_COPY.archive_not_activated);
    const kept = run(
      [{ type: "loaded", result: { ok: false, error: "unreachable", status: 0, currentRevision: null } }],
      loaded,
    );
    expect(kept.status).toBe("loaded");
    expect(kept.saved).toBe(loaded.saved);
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
        return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
      }),
    );
    return calls;
  }

  it("reads the policy through its BFF route", async () => {
    const calls = stub(200, envelope());
    const result = await fetchSourcePolicy(WS, SOURCE);
    expect(calls[0].url).toBe(`/api/workspaces/${WS}/sources/${SOURCE}/policy`);
    expect(calls[0].init?.method ?? "GET").toBe("GET");
    expect(result.ok && result.policy.revision).toBe(4);
  });

  it("PUTs the body unchanged and keeps a conflict's revision", async () => {
    const calls = stub(409, { error: "policy_conflict", current_revision: 5 });
    const body = {
      schema_version: 1,
      expected_revision: 4,
      minimum_age_days: 30,
      include_credited: false,
      include_unknown: true,
    };
    expect(await saveSourcePolicy(WS, SOURCE, body)).toEqual({
      ok: false,
      error: "policy_conflict",
      status: 409,
      currentRevision: 5,
    });
    expect(calls[0].init?.method).toBe("PUT");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual(body);
  });
});
