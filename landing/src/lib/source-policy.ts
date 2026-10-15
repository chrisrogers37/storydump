import { callBff, postJson } from "./bff";
import { sourceRefusalCopy, type AgeBounds } from "./source-refusals";

/**
 * An Instagram past-posts source's policy:
 * how old a post must be before it may be picked, and whether posts that
 * credit someone else, or whose attribution is unknown, may be picked. GET
 * and a successful PUT answer the same envelope, which the BFF forwards whole.
 *
 * THE SERVER SAYS WHAT IS EDITABLE AND WITHIN WHAT LIMITS. The fields drawn
 * are the ones in `capabilities.editable`, the age's limits are `bounds`, and
 * the age in effect is `effective`: the saved age, or more when the workspace's
 * repost window is longer. Nothing here restates a default or a limit, and no
 * ranking, percentile or destination control is drawn until the API offers
 * one.
 */

export type PolicyField = "minimum_age_days" | "include_credited" | "include_unknown" | "carousel_slides";
const FIELDS: readonly PolicyField[] = ["minimum_age_days", "include_credited", "include_unknown", "carousel_slides"];
export type CarouselSlides = "all" | "first";

export type SourcePolicy = {
  schemaVersion: number;
  revision: number;
  minimumAgeDays: number;
  includeCredited: boolean;
  includeUnknown: boolean;
  carouselSlides: CarouselSlides | null;
  carouselChoices: CarouselSlides[];
  /** The age selection applies; null from an answer without it. */
  effectiveMinimumAgeDays: number | null;
  ageBounds: AgeBounds | null;
  /** The fields this editor may draw, in the API's order. */
  editable: PolicyField[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const isWhole = (value: unknown): value is number => Number.isSafeInteger(value);

function readBounds(value: unknown): AgeBounds | null {
  if (!isRecord(value) || !isWhole(value.min) || !isWhole(value.max) || value.min > value.max) {
    return null;
  }
  return { min: value.min, max: value.max };
}

/** The policy envelope as the editor's model, or null when it is not one. */
export function parseSourcePolicy(data: unknown): SourcePolicy | null {
  if (!isRecord(data) || !isWhole(data.schema_version) || !isWhole(data.revision)) return null;
  const policy = data.policy;
  if (
    !isRecord(policy) ||
    !isWhole(policy.minimum_age_days) ||
    typeof policy.include_credited !== "boolean" ||
    typeof policy.include_unknown !== "boolean"
  ) {
    return null;
  }
  const effective = isRecord(data.effective) ? data.effective.minimum_age_days : null;
  const bounds = isRecord(data.bounds) ? readBounds(data.bounds.minimum_age_days) : null;
  const editable =
    isRecord(data.capabilities) && Array.isArray(data.capabilities.editable)
      ? data.capabilities.editable
      : [];
  const rawChoices = isRecord(data.choices) ? data.choices.carousel_slides : null;
  const carouselChoices: CarouselSlides[] = Array.isArray(rawChoices)
    ? rawChoices.filter((choice): choice is CarouselSlides => choice === "all" || choice === "first")
    : [];
  const carouselSlides = policy.carousel_slides;
  if (data.schema_version >= 2 && (carouselSlides !== "all" && carouselSlides !== "first")) return null;
  return {
    schemaVersion: data.schema_version,
    revision: data.revision,
    minimumAgeDays: policy.minimum_age_days,
    includeCredited: policy.include_credited,
    includeUnknown: policy.include_unknown,
    carouselSlides: carouselSlides === "all" || carouselSlides === "first" ? carouselSlides : null,
    carouselChoices,
    effectiveMinimumAgeDays: isWhole(effective) ? effective : null,
    ageBounds: bounds,
    editable: editable.filter((f): f is PolicyField => FIELDS.includes(f as PolicyField)),
  };
}

/** The values being edited, against the revision they were read at. The age is typed text. */
export type PolicyDraft = {
  basedOn: number;
  minimumAgeDays: string;
  includeCredited: boolean;
  includeUnknown: boolean;
  carouselSlides: CarouselSlides | null;
};

export function policyDraft(policy: SourcePolicy): PolicyDraft {
  return {
    basedOn: policy.revision,
    minimumAgeDays: String(policy.minimumAgeDays),
    includeCredited: policy.includeCredited,
    includeUnknown: policy.includeUnknown,
    carouselSlides: policy.carouselSlides,
  };
}

/** Whether the draft says anything the saved values do not. */
export function policyDraftChanged(draft: PolicyDraft, policy: SourcePolicy): boolean {
  return (
    draft.minimumAgeDays.trim() !== String(policy.minimumAgeDays) ||
    draft.includeCredited !== policy.includeCredited ||
    draft.includeUnknown !== policy.includeUnknown ||
    draft.carouselSlides !== policy.carouselSlides
  );
}

/** The PUT body: the schema, the revision read, and the three v1 fields. */
export type PolicyWrite = {
  schema_version: number;
  expected_revision: number;
  minimum_age_days: number;
  include_credited: boolean;
  include_unknown: boolean;
  carousel_slides?: CarouselSlides;
};

export type PolicyDraftCheck =
  | { ok: true; body: PolicyWrite }
  | { ok: false; error: "age_not_whole" | "age_out_of_bounds" | "invalid_carousel_choice" };

/**
 * Whether the draft can be saved, and the body it saves. The age is a whole
 * number of days (the API refuses a decimal) within the server's bounds. A
 * field the API does not offer for editing is sent as it is saved: the v1
 * request names all three, and this editor never changes one it did not draw.
 */
export function checkPolicyDraft(draft: PolicyDraft, policy: SourcePolicy): PolicyDraftCheck {
  const editable = new Set(policy.editable);
  let age = policy.minimumAgeDays;
  if (editable.has("minimum_age_days")) {
    const typed = draft.minimumAgeDays.trim();
    if (!/^\d{1,9}$/.test(typed)) return { ok: false, error: "age_not_whole" };
    age = Number(typed);
    const bounds = policy.ageBounds;
    if (bounds && (age < bounds.min || age > bounds.max)) {
      return { ok: false, error: "age_out_of_bounds" };
    }
  }
  const slideChoice = editable.has("carousel_slides") ? draft.carouselSlides : policy.carouselSlides;
  if (policy.schemaVersion >= 2 && (!slideChoice || !policy.carouselChoices.includes(slideChoice))) {
    return { ok: false, error: "invalid_carousel_choice" };
  }
  return {
    ok: true,
    body: {
      schema_version: policy.schemaVersion,
      expected_revision: draft.basedOn,
      minimum_age_days: age,
      include_credited: editable.has("include_credited") ? draft.includeCredited : policy.includeCredited,
      include_unknown: editable.has("include_unknown") ? draft.includeUnknown : policy.includeUnknown,
      ...(policy.schemaVersion >= 2 && slideChoice ? { carousel_slides: slideChoice } : {}),
    },
  };
}

function days(n: number): string {
  return `${n.toLocaleString("en-US")} ${n === 1 ? "day" : "days"}`;
}

function range(bounds: AgeBounds): string {
  return `from ${bounds.min.toLocaleString("en-US")} to ${bounds.max.toLocaleString("en-US")}`;
}

/** The age's limits as the server sent them, said beside the field. */
export function ageBoundsCopy(bounds: AgeBounds | null): string | null {
  return bounds ? `Any whole number of days ${range(bounds)}.` : null;
}

/** The sentence for an age the editor will not send. */
export function ageCheckCopy(bounds: AgeBounds | null): string {
  return bounds ? `Type a whole number of days ${range(bounds)}.` : "Type a whole number of days.";
}

/**
 * The saved age and the age in effect, side by side. When the repost window
 * raises it, that is said, so a person who saved 90 days is not puzzled by 120.
 */
export function ageFactsCopy(policy: SourcePolicy): string {
  const saved = `Saved: ${days(policy.minimumAgeDays)}.`;
  const effective = policy.effectiveMinimumAgeDays;
  if (effective === null) return saved;
  if (effective > policy.minimumAgeDays) {
    return `${saved} In effect: ${days(effective)}, because the workspace's repost window is longer.`;
  }
  return `${saved} In effect: ${days(effective)}.`;
}

/** The three v1 values as words, for the read-only view and a conflict's comparison. */
export function policyValueText(field: PolicyField, values: PolicyDraft): string {
  switch (field) {
    case "minimum_age_days": {
      const typed = values.minimumAgeDays.trim();
      return /^\d+$/.test(typed) ? days(Number(typed)) : typed || "blank";
    }
    case "include_credited":
      return values.includeCredited ? "On" : "Off";
    case "include_unknown":
      return values.includeUnknown ? "On" : "Off";
    case "carousel_slides":
      return values.carouselSlides === "first" ? "First slide only" : "All slides";
  }
}

export const POLICY_FIELD_COPY: Record<PolicyField, { label: string; description: string }> = {
  minimum_age_days: {
    label: "Minimum age of a post (days)",
    description: "Posts younger than this are not picked.",
  },
  include_credited: {
    label: "Include posts that credit someone else",
    description: "Off: a post that credits another account is not picked.",
  },
  include_unknown: {
    label: "Include posts with unknown attribution",
    description: "Off: a post whose attribution could not be told is not picked.",
  },
  carousel_slides: {
    label: "Carousel slides",
    description: "Choose whether every slide or only the first may be picked.",
  },
};

export const MEMBER_POLICY_COPY = "Ask an admin to change these settings.";

// ── The editor's states ─────────────────────────────────────────────────────

/**
 * The editor's states, as one reducer (no test here presses a button). A
 * refusal never drops the draft. A `policy_conflict` keeps it against the
 * revision it was read at, and the editor reads the saved values again; while
 * the two revisions differ the draft cannot be saved, and the saved values are
 * shown beside it until the person carries the draft over or drops it.
 */
export type PolicyFlow = {
  status: "idle" | "loading" | "loaded" | "failed";
  saved: SourcePolicy | null;
  draft: PolicyDraft | null;
  saving: boolean;
  /** A save met a conflict; the draft waits on the saved values read again. */
  conflict: boolean;
  error: string | null;
  announcement: string;
};

export const INITIAL_POLICY_FLOW: PolicyFlow = {
  status: "idle",
  saved: null,
  draft: null,
  saving: false,
  conflict: false,
  error: null,
  announcement: "",
};

export type PolicyCallResult =
  | { ok: true; policy: SourcePolicy }
  | { ok: false; error: string; status: number; currentRevision: number | null };

export type PolicyFlowEvent =
  | { type: "load_started" }
  | { type: "loaded"; result: PolicyCallResult }
  | { type: "edited"; patch: Partial<Omit<PolicyDraft, "basedOn">> }
  | { type: "save_started" }
  | { type: "save_answered"; result: PolicyCallResult }
  | { type: "rebased" }
  | { type: "discarded" };

/** Whether the kept draft was read at another revision than the saved values shown. */
export function policyDraftStale(flow: PolicyFlow): boolean {
  return flow.draft !== null && flow.saved !== null && flow.draft.basedOn !== flow.saved.revision;
}

function policyRefused(result: Extract<PolicyCallResult, { ok: false }>, saved: SourcePolicy | null) {
  return sourceRefusalCopy(result.error, result.status, {
    surface: "policy",
    bounds: saved?.ageBounds ?? null,
  });
}

export function policyFlowReducer(flow: PolicyFlow, event: PolicyFlowEvent): PolicyFlow {
  switch (event.type) {
    case "load_started":
      return flow.status === "loading" ? flow : { ...flow, status: flow.saved ? flow.status : "loading" };

    case "loaded": {
      const { result } = event;
      if (!result.ok) {
        return flow.saved
          ? { ...flow, error: policyRefused(result, flow.saved) }
          : { ...flow, status: "failed", error: policyRefused(result, null) };
      }
      // A reload after a conflict keeps the draft as typed; a first read opens one.
      return {
        ...flow,
        status: "loaded",
        saved: result.policy,
        draft: flow.draft ?? policyDraft(result.policy),
        error: flow.conflict ? flow.error : null,
      };
    }

    case "edited":
      if (!flow.draft || flow.saving) return flow;
      return { ...flow, draft: { ...flow.draft, ...event.patch } };

    case "save_started":
      if (!flow.draft || flow.saving) return flow;
      return { ...flow, saving: true, error: null, announcement: "" };

    case "save_answered": {
      const { result } = event;
      if (result.ok) {
        return {
          ...flow,
          saved: result.policy,
          draft: policyDraft(result.policy),
          saving: false,
          conflict: false,
          error: null,
          announcement: "Past posts settings saved. They apply from the next selection.",
        };
      }
      if (result.error === "policy_conflict") {
        return {
          ...flow,
          saving: false,
          conflict: true,
          error: policyRefused(result, flow.saved),
          announcement: "These settings changed while you were editing. Your changes are kept.",
        };
      }
      return { ...flow, saving: false, error: policyRefused(result, flow.saved), announcement: "" };
    }

    case "rebased":
      if (!flow.draft || !flow.saved || flow.saving) return flow;
      return {
        ...flow,
        draft: { ...flow.draft, basedOn: flow.saved.revision },
        conflict: false,
        error: null,
        announcement: "Your changes are now on the latest saved settings. Review them, then save.",
      };

    case "discarded":
      if (!flow.saved || flow.saving) return flow;
      return { ...flow, draft: policyDraft(flow.saved), conflict: false, error: null, announcement: "" };
  }
}

// ── The two doors ───────────────────────────────────────────────────────────

function policyPath(workspaceId: string, sourceId: string): string {
  return `/api/workspaces/${workspaceId}/sources/${sourceId}/policy`;
}

async function answer(promise: ReturnType<typeof callBff>): Promise<PolicyCallResult> {
  const result = await promise;
  if (!result.ok) {
    const revision = result.body.current_revision;
    return {
      ok: false,
      error: result.error,
      status: result.status,
      currentRevision: isWhole(revision) && revision >= 0 ? revision : null,
    };
  }
  const policy = parseSourcePolicy(result.data);
  return policy
    ? { ok: true, policy }
    : { ok: false, error: "malformed_response", status: result.status, currentRevision: null };
}

/** Read the saved policy (member floor). */
export function fetchSourcePolicy(workspaceId: string, sourceId: string): Promise<PolicyCallResult> {
  return answer(callBff(policyPath(workspaceId, sourceId)));
}

/** Save the policy (admin floor), against the revision the draft was read at. */
export function saveSourcePolicy(
  workspaceId: string,
  sourceId: string,
  body: PolicyWrite,
): Promise<PolicyCallResult> {
  return answer(callBff(policyPath(workspaceId, sourceId), postJson(body, "PUT")));
}
