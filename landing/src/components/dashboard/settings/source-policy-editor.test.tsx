import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  INITIAL_POLICY_FLOW,
  MEMBER_POLICY_COPY,
  parseSourcePolicy,
  policyFlowReducer,
  type PolicyFlow,
  type PolicyFlowEvent,
  type SourcePolicy,
} from "@/lib/source-policy";
import { PolicyPanel, SourcePolicyEditor } from "./source-policy-editor";

/**
 * "Past posts settings" under an Instagram source, drawn from each state of its
 * flow (`environment: "node"`: markup, not a browser). `PolicyPanel` takes no
 * hooks, so a state is a `flow` and what a person would see, hear and be able to
 * press is read back from the markup. What presses and dispatches is
 * `source-policy.test.ts`'s.
 */

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

const carouselPolicy = () => policy({
  schema_version: 2,
  policy: { minimum_age_days: 90, include_credited: false, include_unknown: false, carousel_slides: "first" },
  choices: { carousel_slides: ["all", "first"] },
  capabilities: { editable: ["minimum_age_days", "include_credited", "include_unknown", "carousel_slides"] },
});

function policy(over: Record<string, unknown> = {}): SourcePolicy {
  const parsed = parseSourcePolicy(envelope(over));
  if (!parsed) throw new Error("fixture did not parse");
  return parsed;
}

const ok = (saved: SourcePolicy): PolicyFlowEvent => ({
  type: "loaded",
  result: { ok: true, policy: saved },
});

const flowOf = (events: PolicyFlowEvent[], from: PolicyFlow = INITIAL_POLICY_FLOW): PolicyFlow =>
  events.reduce(policyFlowReducer, from);

const loaded = (saved = policy()) => flowOf([{ type: "load_started" }, ok(saved)]);

function draw(flow: PolicyFlow, editable = true): string {
  return renderToStaticMarkup(
    createElement(PolicyPanel, {
      flow,
      editable,
      ids: "ID",
      onEdit: () => {},
      onSave: () => {},
      onRebase: () => {},
      onDiscard: () => {},
      onRetry: () => {},
    }),
  );
}

// ── Reading markup ─────────────────────────────────────────────────────────

/**
 * The visible text: inline spans dropped as they are written, every other tag
 * a space, entities read, white space folded.
 */
function textOf(html: string): string {
  return html
    .replace(/<\/?span[^>]*>/g, "")
    .replace(/<[^>]*>/g, " ")
    .replace(/&#x27;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ")
    .trim();
}

/** A value that must be there: a markup reader that finds nothing is a failure, not a pass. */
function must<T>(value: T | undefined, what: string): T {
  if (value === undefined) throw new Error(`no ${what} in the markup`);
  return value;
}

function attr(tag: string, name: string): string | undefined {
  return tag.match(new RegExp(`\\s${name}="([^"]*)"`))?.[1];
}

const isDisabled = (tag: string) => /\sdisabled(?:=""|\s|\/?>)/.test(tag);

/** The opening tag of the button that holds exactly this label, or "". */
function button(html: string, label: string): string {
  const end = html.indexOf(`>${label}</button>`);
  return end < 0 ? "" : html.slice(html.lastIndexOf("<button", end), end + 1);
}

const switches = (html: string) => html.match(/<button[^>]*role="switch"[^>]*>/g) ?? [];
const inputs = (html: string) =>
  (html.match(/<input[^>]*>/g) ?? []).filter((tag) => attr(tag, "type") !== "checkbox");

/** The label for this control: its opening tag, through its closing one. */
function label(html: string, controlId: string): string {
  const found = html.match(new RegExp(`<label[^>]*\\sfor="${controlId}"[^>]*>[\\s\\S]*?</label>`));
  if (!found) throw new Error(`no label for ${controlId}`);
  return found[0];
}

/** The text of the paragraph with this id. */
function textAt(html: string, id: string): string {
  const found = html.match(new RegExp(`id="${id}"[^>]*>([\\s\\S]*?)</p>`));
  if (!found) throw new Error(`no paragraph with id ${id}`);
  return textOf(found[1]);
}

describe("version 2 carousel choice", () => {
  it("shows the server's two choices and the saved selection to admins and members", () => {
    const admin = draw(loaded(carouselPolicy()));
    expect(admin).toContain('id="ID-carousel"');
    expect(admin).toContain('<option value="all">All slides</option>');
    expect(admin).toContain('<option value="first" selected="">First slide only</option>');
    const member = draw(loaded(carouselPolicy()), false);
    expect(textOf(member)).toContain("Carousel slides First slide only");
    expect(member).not.toContain('id="ID-carousel"');
  });
});

describe("an admin edits what the API says is editable", () => {
  it("draws the age and the two opt-ins as three controls, each named and described", () => {
    const html = draw(loaded());
    // The age: a labelled number field, described by its help and by what is saved.
    expect(label(html, "ID-age")).toContain(">Minimum age of a post (days)</label>");
    const [age] = inputs(html);
    expect(attr(age, "id")).toBe("ID-age");
    expect(attr(age, "inputMode")).toBe("numeric");
    expect(attr(age, "value")).toBe("90");
    const described = attr(age, "aria-describedby")!.split(" ");
    expect(described).toEqual(["ID-age-help", "ID-age-facts"]);
    expect(textAt(html, "ID-age-help")).toBe(
      "Posts younger than this are not picked. Any whole number of days from 0 to 36,500.",
    );
    expect(textAt(html, "ID-age-facts")).toBe("Saved: 90 days. In effect: 90 days.");

    // Two switches, each named by its own label and described by its own help.
    expect(switches(html)).toHaveLength(2);
    const credited = must(switches(html)[0], "first switch");
    const unknown = must(switches(html)[1], "second switch");
    expect(textAt(html, attr(credited, "aria-labelledby")!)).toBe("Include posts that credit someone else");
    expect(textAt(html, attr(credited, "aria-describedby")!)).toBe(
      "Off: a post that credits another account is not picked.",
    );
    expect(textAt(html, attr(unknown, "aria-labelledby")!)).toBe("Include posts with unknown attribution");
    expect(textAt(html, attr(unknown, "aria-describedby")!)).toBe(
      "Off: a post whose attribution could not be told is not picked.",
    );
  });

  it("sets the age's label as a block of its own, above a narrow input, never inline beside it", () => {
    // A plain `<label class="text-sm font-medium">` is inline, so at 768px and up
    // "Minimum age of a post (days)" would sit on the input's line with no gap.
    // The repo's `Label` is a flex block, as every other field's is.
    const tag = must(label(draw(loaded()), "ID-age").match(/<label[^>]*>/)?.[0], "age label");
    expect(attr(tag, "data-slot")).toBe("label");
    const classes = (attr(tag, "class") ?? "").split(" ");
    expect(classes).toContain("flex");
    expect(classes).toContain("text-sm");
    expect(classes).toContain("font-medium");
    // The input it labels stays the narrow one the label must not share a line with.
    expect((attr(inputs(draw(loaded()))[0], "class") ?? "").split(" ")).toContain("w-32");
  });

  it("keeps the two opt-ins independent: each shows its own saved value", () => {
    const html = draw(loaded(policy({ policy: { minimum_age_days: 90, include_credited: true, include_unknown: false } })));
    expect(switches(html).map((tag) => attr(tag, "aria-checked"))).toEqual(["true", "false"]);
    const flipped = draw(loaded(policy({ policy: { minimum_age_days: 90, include_credited: false, include_unknown: true } })));
    expect(switches(flipped).map((tag) => attr(tag, "aria-checked"))).toEqual(["false", "true"]);
  });

  it("draws only the fields the API lists as editable, and no ranking, percentile or destination control", () => {
    const ageOnly = draw(loaded(policy({ capabilities: { editable: ["minimum_age_days"] } })));
    expect(inputs(ageOnly)).toHaveLength(1);
    expect(switches(ageOnly)).toEqual([]);

    const creditedOnly = draw(loaded(policy({ capabilities: { editable: ["include_credited"] } })));
    expect(inputs(creditedOnly)).toEqual([]);
    expect(switches(creditedOnly)).toHaveLength(1);
    expect(textOf(creditedOnly)).toContain("Include posts that credit someone else");
    expect(textOf(creditedOnly)).not.toContain("unknown attribution");

    // Fields this page has no words for are not drawn, whatever the API lists.
    const future = draw(
      loaded(policy({ capabilities: { editable: ["minimum_age_days", "ranking", "percentile", "destination"] } })),
    );
    expect(inputs(future)).toHaveLength(1);
    expect(switches(future)).toEqual([]);
    for (const html of [future, draw(loaded())]) {
      expect(html).not.toMatch(/ranking|percentile|destination|<select/i);
    }
  });

  it("says the saved age beside the age in effect, and why they differ", () => {
    const same = draw(loaded());
    expect(textAt(same, "ID-age-facts")).toBe("Saved: 90 days. In effect: 90 days.");
    const raised = draw(loaded(policy({ effective: { minimum_age_days: 120 } })));
    expect(textAt(raised, "ID-age-facts")).toBe(
      "Saved: 90 days. In effect: 120 days, because the workspace's repost window is longer.",
    );
    expect(textAt(draw(loaded(policy({ policy: { minimum_age_days: 1, include_credited: false, include_unknown: false }, effective: { minimum_age_days: 1 } }))), "ID-age-facts")).toBe(
      "Saved: 1 day. In effect: 1 day.",
    );
  });

  it("holds the age to the server's bounds, says them, and will not save an age outside them", () => {
    const saved = policy({ bounds: { minimum_age_days: { min: 7, max: 365 } } });
    const typed = (text: string) => flowOf([{ type: "edited", patch: { minimumAgeDays: text } }], loaded(saved));

    expect(textAt(draw(typed("30")), "ID-age-help")).toContain("Any whole number of days from 7 to 365.");
    const fine = draw(typed("30"));
    expect(isDisabled(button(fine, "Save settings"))).toBe(false);
    expect(attr(inputs(fine)[0], "aria-invalid")).toBeUndefined();
    expect(fine).not.toContain("ID-age-error");

    for (const text of ["6", "366", "40000", "", "12.5", "-3", "abc"]) {
      const html = draw(typed(text));
      expect(isDisabled(button(html, "Save settings")), text).toBe(true);
      const [input] = inputs(html);
      expect(attr(input, "aria-invalid"), text).toBe("true");
      expect(attr(input, "aria-describedby"), text).toContain("ID-age-error");
      expect(textAt(html, "ID-age-error"), text).toBe("Type a whole number of days from 7 to 365.");
    }
  });

  it("offers Save only once something changed, and says there is something unsaved", () => {
    const untouched = draw(loaded());
    expect(isDisabled(button(untouched, "Save settings"))).toBe(true);
    expect(textOf(untouched)).not.toContain("Unsaved changes");

    const changed = draw(flowOf([{ type: "edited", patch: { includeCredited: true } }], loaded()));
    expect(isDisabled(button(changed, "Save settings"))).toBe(false);
    expect(textOf(changed)).toContain("Unsaved changes");
  });

  it("locks every field while it saves, and says so", () => {
    const html = draw(flowOf([{ type: "edited", patch: { minimumAgeDays: "30" } }, { type: "save_started" }], loaded()));
    expect(isDisabled(inputs(html)[0])).toBe(true);
    for (const tag of switches(html)) expect(isDisabled(tag)).toBe(true);
    expect(isDisabled(button(html, "Saving..."))).toBe(true);
  });

  it("announces a save in one polite place, and shows what the server answered", () => {
    const saved = policy({ revision: 5, policy: { minimum_age_days: 30, include_credited: false, include_unknown: false } });
    const html = draw(
      flowOf([{ type: "edited", patch: { minimumAgeDays: "30" } }, { type: "save_started" }, { type: "save_answered", result: { ok: true, policy: saved } }], loaded()),
    );
    const regions = html.match(/<p[^>]*aria-live="polite"[^>]*>[\s\S]*?<\/p>/g) ?? [];
    expect(regions).toHaveLength(1);
    expect(regions[0]).toContain('role="status"');
    expect(textOf(must(regions[0], "live region"))).toBe(
      "Past posts settings saved. They apply from the next selection.",
    );
    // Said to the eye as well as the ear once there is something to say.
    expect(regions[0]).not.toContain("sr-only");
    expect(attr(inputs(html)[0], "value")).toBe("30");
    expect(textAt(html, "ID-age-facts")).toContain("Saved: 30 days.");
    // Quiet until there is something to say.
    const quiet = (draw(loaded()).match(/<p[^>]*aria-live="polite"[^>]*>[\s\S]*?<\/p>/g) ?? [])[0];
    expect(quiet).toContain("sr-only");
  });

  it("says it beside Save, under the fields, so what is said never pushes them down", () => {
    // Above the fields, the sentence turning from `sr-only` to a visible line
    // would move the age field and the switches down. Beside Save, nothing it
    // follows can move.
    const saved = policy({ revision: 5, policy: { minimum_age_days: 30, include_credited: false, include_unknown: false } });
    const announced = draw(
      flowOf([{ type: "edited", patch: { minimumAgeDays: "30" } }, { type: "save_started" }, { type: "save_answered", result: { ok: true, policy: saved } }], loaded()),
    );
    for (const html of [announced, draw(loaded())]) {
      const region = html.indexOf('<p role="status" aria-live="polite"');
      expect(region).toBeGreaterThan(-1);
      expect(region).toBeGreaterThan(html.indexOf('id="ID-age"'));
      expect(region).toBeGreaterThan(html.lastIndexOf('role="switch"'));
      expect(region).toBeGreaterThan(html.indexOf(">Save settings</button>"));
      // Nothing but the end of the Save row and the panel follows it.
      expect(html.slice(html.indexOf("</p>", region) + "</p>".length)).toBe("</div></div>");
    }
  });
});

describe("a member reads the same values and cannot change them", () => {
  it("shows each editable field's saved value, the age in effect, and who to ask", () => {
    const html = draw(loaded(policy({ policy: { minimum_age_days: 90, include_credited: true, include_unknown: false }, effective: { minimum_age_days: 120 } })), false);
    expect(inputs(html)).toEqual([]);
    expect(switches(html)).toEqual([]);
    expect(html).not.toContain("<button");
    const text = textOf(html);
    expect(text).toContain("Minimum age of a post (days) 90 days");
    expect(text).toContain("Include posts that credit someone else On");
    expect(text).toContain("Include posts with unknown attribution Off");
    expect(text).toContain("In effect: 120 days, because the workspace's repost window is longer.");
    expect(text).toContain(MEMBER_POLICY_COPY);
    expect(MEMBER_POLICY_COPY).toBe("Ask an admin to change these settings.");
  });

  it("lists only the fields the API lists", () => {
    const text = textOf(draw(loaded(policy({ capabilities: { editable: ["minimum_age_days"] } })), false));
    expect(text).toContain("Minimum age of a post (days)");
    expect(text).not.toContain("credit someone else");
    expect(text).not.toContain("unknown attribution");
  });

  it("is a member's view because of the role, and an admin's of the same policy has the controls", () => {
    const flow = loaded();
    expect(switches(draw(flow, false))).toEqual([]);
    expect(switches(draw(flow, true))).toHaveLength(2);
  });
});

describe("a refusal never takes the draft away", () => {
  const typed = flowOf([{ type: "edited", patch: { minimumAgeDays: "30", includeCredited: true } }], loaded());
  const refused = (error: string, status: number, currentRevision: number | null = null) =>
    flowOf([{ type: "save_started" }, { type: "save_answered", result: { ok: false, error, status, currentRevision } }], typed);

  it.each([
    ["invalid_source_policy", 400, "Those settings were refused: the minimum age must be a whole number of days from 0 to 36,500. Nothing changed."],
    ["http_403", 403, "You need to be an admin of this workspace to change this. Nothing changed."],
    ["unsupported_source_policy", 400, /of a version this page cannot change\. Reload the page/],
    ["source_not_found", 404, /no longer connected\. Reload the page/],
    ["target_router_unreachable", 503, /Nothing changed/],
  ])("says %s as an alert, and keeps what was typed", (error, status, expected) => {
    const html = draw(refused(error, status));
    const alert = html.match(/<div role="alert"[^>]*>([\s\S]*?)<\/div>/);
    expect(alert, error).not.toBeNull();
    if (typeof expected === "string") expect(textOf(alert![1])).toBe(expected);
    else expect(textOf(alert![1])).toMatch(expected);
    // The draft: the typed age, and the opt-in the person switched on.
    expect(attr(inputs(html)[0], "value")).toBe("30");
    expect(switches(html).map((tag) => attr(tag, "aria-checked"))).toEqual(["true", "false"]);
    // And it can be tried again.
    expect(isDisabled(button(html, "Save settings"))).toBe(false);
  });

  it("holds a conflicted draft apart from the saved values until it is read again", () => {
    const html = draw(refused("policy_conflict", 409, 5));
    const text = textOf(html);
    expect(text).toContain("Someone saved these settings while you were editing.");
    expect(text).toContain("The saved settings have not been read again yet. Your changes are kept.");
    expect(button(html, "Read the saved settings again")).not.toBe("");
    expect(button(html, "Discard my changes")).not.toBe("");
    expect(isDisabled(button(html, "Save settings"))).toBe(true);
    expect(attr(inputs(html)[0], "value")).toBe("30");
  });

  it("shows the draft beside the saved values once they are read, and saves only after it is carried over", () => {
    const conflicted = refused("policy_conflict", 409, 5);
    const newer = policy({
      revision: 5,
      policy: { minimum_age_days: 60, include_credited: false, include_unknown: true },
    });
    const reread = flowOf([ok(newer)], conflicted);
    const html = draw(reread);
    const text = textOf(html);
    expect(text).toContain("Your changes, beside the settings saved now");
    expect(text).toContain("Minimum age of a post (days): yours 30 days · saved 60 days");
    expect(text).toContain("Include posts that credit someone else: yours On · saved Off");
    expect(text).toContain("Include posts with unknown attribution: yours Off · saved On");
    expect(isDisabled(button(html, "Save settings"))).toBe(true);
    expect(button(html, "Keep my changes on these settings")).not.toBe("");
    expect(button(html, "Read the saved settings again")).toBe("");
    // The typed draft is untouched by the re-read.
    expect(attr(inputs(html)[0], "value")).toBe("30");

    // Carried over by choice, it is a draft on the new revision and can be saved.
    const kept = draw(flowOf([{ type: "rebased" }], reread));
    expect(textOf(kept)).not.toContain("beside the settings saved now");
    expect(isDisabled(button(kept, "Save settings"))).toBe(false);
    expect(attr(inputs(kept)[0], "value")).toBe("30");

    // Or dropped by choice, back to what is saved now.
    const dropped = draw(flowOf([{ type: "discarded" }], reread));
    expect(attr(inputs(dropped)[0], "value")).toBe("60");
    expect(textOf(dropped)).not.toContain("beside the settings saved now");
  });
});

describe("before there is anything to edit", () => {
  it("says it is loading, with no control", () => {
    const html = draw(flowOf([{ type: "load_started" }]));
    expect(textOf(html)).toBe("Loading past posts settings…");
    expect(html).not.toContain("<button");
    expect(html).not.toContain("<input");
  });

  it("says why a first read failed, and offers to try again", () => {
    const html = draw(
      flowOf([
        { type: "load_started" },
        { type: "loaded", result: { ok: false, error: "target_router_unreachable", status: 503, currentRevision: null } },
      ]),
    );
    expect(html).toMatch(/<div role="alert"/);
    expect(textOf(html)).toContain("Nothing changed — try again shortly.");
    expect(button(html, "Try again")).not.toBe("");
    expect(inputs(html)).toEqual([]);
  });

  it("is a failed read, not a blank panel, for a policy that is not one", () => {
    const html = draw(
      flowOf([
        { type: "load_started" },
        { type: "loaded", result: { ok: false, error: "malformed_response", status: 200, currentRevision: null } },
      ]),
    );
    expect(textOf(html)).toContain("may or may not have been saved");
    expect(button(html, "Try again")).not.toBe("");
  });
});

describe("on a narrow screen, the review's buttons wrap instead of running past the card", () => {
  // These tests cannot measure a 320px screen, so they pin the classes:
  // `Button` is `shrink-0 whitespace-nowrap`, so without them "Keep my
  // changes on these settings" runs past the card at 320px. These are the
  // classes that stop it.
  const conflicted = flowOf(
    [
      { type: "edited", patch: { minimumAgeDays: "30" } },
      { type: "save_started" },
      { type: "save_answered", result: { ok: false, error: "policy_conflict", status: 409, currentRevision: 5 } },
    ],
    loaded(),
  );
  const classesOf = (tag: string) => (attr(tag, "class") ?? "").split(" ");

  it.each([
    ["Keep my changes on these settings", flowOf([ok(policy({ revision: 5 }))], conflicted)],
    ["Read the saved settings again", conflicted],
  ])("lets %s wrap and shrink", (label, flow) => {
    const classes = classesOf(button(draw(flow), label));
    expect(classes, label).toEqual(expect.arrayContaining(["whitespace-normal", "min-w-0", "shrink", "h-auto"]));
    expect(classes, label).not.toContain("whitespace-nowrap");
    expect(classes, label).not.toContain("shrink-0");
    expect(classesOf(button(draw(flow), "Discard my changes"))).toContain("whitespace-normal");
  });

  it("leaves the short Save button as Button makes it", () => {
    expect(classesOf(button(draw(conflicted), "Save settings"))).toContain("whitespace-nowrap");
  });
});

describe("the collapsed toggle under a source's row", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("is closed, named for its source, controls a hidden panel, and reads nothing until opened", () => {
    const fetched = vi.fn();
    vi.stubGlobal("fetch", fetched);
    const html = renderToStaticMarkup(
      createElement(SourcePolicyEditor, {
        workspaceId: "ws-1",
        sourceId: "22222222-2222-4222-8222-222222222222",
        sourceName: "Your Instagram @example_handle",
        editable: true,
      }),
    );
    const toggle = button(html, "Past posts settings");
    expect(toggle).not.toBe("");
    expect(attr(toggle, "aria-label")).toBe("Past posts settings for Your Instagram @example_handle");
    // The accessible name keeps the visible words in it.
    expect(attr(toggle, "aria-label")).toContain("Past posts settings");
    expect(attr(toggle, "aria-expanded")).toBe("false");
    const panel = attr(toggle, "aria-controls")!;
    expect(html).toMatch(new RegExp(`<div id="${panel}" hidden=""></div>`));
    expect(fetched).not.toHaveBeenCalled();
    expect(inputs(html)).toEqual([]);
  });
});
