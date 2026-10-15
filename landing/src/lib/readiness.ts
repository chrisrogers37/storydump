import type { BadgeTone } from "@/components/dashboard/tone";
import { RESOLVED_IN, nextSetupStep, type SetupProgress, type SetupStep } from "./conditions";
import { MEDIA_CHECKED_COPY, bareHandle, type DestinationSourceView } from "./source-mix";

/**
 * Destination readiness, as the overview says it (the mix resource's
 * `destinations` and `guidance`).
 *
 * THE SERVER DECIDES, THIS SAYS. A converted workspace's mix resource lists
 * every listed account with its `state`, `tone` and `remedy`, computed from the
 * same facts selection and runway use, and a `guidance` rollup that orders the
 * accounts still being set up. Nothing here derives a state, counts candidates
 * or decides what is ready: it labels the state through a closed table, maps
 * the tone onto the dashboard's badge tones, and turns the remedy into a link
 * (an admin) or an ask (a member).
 *
 * Protocol 1 has none of this, and its setup step stays row existence
 * (`nextSetupStep`); `overviewSetup` is the one switch between the two.
 */

export type ReadinessState =
  | "inactive"
  | "needs_connection"
  | "needs_reconnect"
  | "sources_off"
  | "no_assignable_source"
  | "indexing"
  | "zero_eligible"
  | "ready"
  | "stalled"
  | "error";

export type ReadinessDestination = {
  accountId: string;
  handle: string | null;
  displayName: string | null;
  /** As sent; labelled through `STATE_LABEL`. */
  state: string;
  tone: BadgeTone;
  remedy: string | null;
  partialCoverage: boolean;
  /** Metadata-matching posts for this account, never verified media. */
  candidates: number | null;
  leadingExclusion: { reason: string; count: number } | null;
  /** The oldest post indexed from an archive it draws on: coverage evidence only. */
  oldestObservedAt: string | null;
  /** An enabled Drive folder it draws on is in `error`: where its retry is done. */
  driveError: boolean;
};

export type Guidance = {
  /** Accounts that are not inactive. */
  active: number;
  ready: number;
  intentional: "all_inactive" | "all_sources_off" | null;
  /** The accounts still being set up, in the server's order. */
  incomplete: string[];
};

export type Readiness = { destinations: ReadinessDestination[]; guidance: Guidance };

/**
 * The server's tone names onto the dashboard's badge tones (`tone.ts`): a
 * closed table, so the contract's five tones are named here once. A tone this
 * build does not know is `attention`, as `destinationStateBadge` treats a
 * state it does not know: worth looking at, never "nothing to see".
 */
const TONE: Record<string, BadgeTone> = {
  inert: "inert",
  attention: "attention",
  progress: "progress",
  active: "active",
  problem: "problem",
};

export function readinessTone(tone: unknown): BadgeTone {
  return typeof tone === "string" && Object.hasOwn(TONE, tone) ? TONE[tone] : "attention";
}

/** Each state's badge, in words. A `Record` on the union: a new state needs one. */
const STATE_LABEL: Record<ReadinessState, string> = {
  inactive: "Inactive",
  needs_connection: "Not connected",
  needs_reconnect: "Reconnect needed",
  sources_off: "Sources Off",
  no_assignable_source: "No source",
  indexing: "Indexing",
  zero_eligible: "No matching posts",
  ready: "Ready",
  stalled: "Indexing stalled",
  error: "Source stopped",
};

export function readinessStateLabel(state: string): string {
  return Object.hasOwn(STATE_LABEL, state) ? STATE_LABEL[state as ReadinessState] : "Unknown state";
}

export const PARTIAL_COVERAGE_BADGE = { label: "Partial coverage", tone: "attention" } as const;

// ── Reading the resource ────────────────────────────────────────────────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function count(value: unknown): number | null {
  return Number.isSafeInteger(value) && (value as number) >= 0 ? (value as number) : null;
}

type RowFacts = { provider: string; enabled: boolean; state: string };

function readDestination(raw: unknown, rows: Map<string, RowFacts>): ReadinessDestination | null {
  if (!isRecord(raw) || typeof raw.account_id !== "string" || typeof raw.state !== "string") {
    return null;
  }
  const leading = raw.leading_exclusion;
  let oldest: string | null = null;
  let driveError = false;
  for (const source of Array.isArray(raw.sources) ? raw.sources : []) {
    if (!isRecord(source) || source.assignable !== true || typeof source.source_id !== "string") {
      continue;
    }
    const row = rows.get(source.source_id);
    if (!row?.enabled) continue;
    const at = source.oldest_observed_at;
    if (row.provider === "instagram_archive" && typeof at === "string" && !Number.isNaN(Date.parse(at))) {
      if (oldest === null || Date.parse(at) < Date.parse(oldest)) oldest = at;
    }
    if (row.provider === "gdrive" && row.state === "error") driveError = true;
  }
  return {
    accountId: raw.account_id,
    handle: bareHandle(raw.handle),
    displayName:
      typeof raw.display_name === "string" && raw.display_name.trim() ? raw.display_name : null,
    state: raw.state,
    tone: readinessTone(raw.tone),
    remedy: typeof raw.remedy === "string" ? raw.remedy : null,
    partialCoverage: raw.partial_coverage === true,
    candidates: count(raw.candidates),
    leadingExclusion:
      isRecord(leading) && typeof leading.reason === "string" && count(leading.count) !== null
        ? { reason: leading.reason, count: leading.count as number }
        : null,
    oldestObservedAt: oldest,
    driveError,
  };
}

function readGuidance(raw: unknown): Guidance | null {
  if (!isRecord(raw)) return null;
  const active = count(raw.active);
  const ready = count(raw.ready);
  const { intentional } = raw;
  if (active === null || ready === null || !Array.isArray(raw.incomplete)) return null;
  if (intentional !== null && intentional !== "all_inactive" && intentional !== "all_sources_off") {
    return null;
  }
  return {
    active,
    ready,
    intentional,
    incomplete: raw.incomplete.flatMap((entry) =>
      isRecord(entry) && typeof entry.account_id === "string" ? [entry.account_id] : [],
    ),
  };
}

/**
 * The readiness a protocol-2 mix resource carries, or null: for protocol 1,
 * and for a protocol-2 answer without a readable `guidance` (an API a slice
 * behind), whose overview keeps its setup step.
 */
export function parseReadiness(data: unknown): Readiness | null {
  if (!isRecord(data) || data.protocol_version !== 2 || !Array.isArray(data.destinations)) {
    return null;
  }
  const guidance = readGuidance(data.guidance);
  if (!guidance) return null;
  const rows = new Map<string, RowFacts>();
  for (const row of Array.isArray(data.rows) ? data.rows : []) {
    if (isRecord(row) && typeof row.source_id === "string") {
      rows.set(row.source_id, {
        provider: typeof row.provider === "string" ? row.provider : "",
        enabled: row.enabled === true,
        state: typeof row.state === "string" ? row.state : "",
      });
    }
  }
  return {
    destinations: data.destinations.flatMap((raw) => {
      const read = readDestination(raw, rows);
      return read ? [read] : [];
    }),
    guidance,
  };
}

// ── Saying it ───────────────────────────────────────────────────────────────

/** Why a matching post is left out, after "because". The reasons are the
 * suitability function's (`fn_source_asset_suitability`); one this page does not know is said plainly. */
const EXCLUSION_COPY: Record<string, string> = {
  too_recent: "they are newer than the minimum age",
  slide_not_first: "this source uses only the first slide of each carousel",
  credited_excluded: "they credit another account, and credited posts are left out",
  attribution_unknown: "who made them is unknown, and such posts are left out",
  locked: "they were posted recently or are held back",
  live_intent: "they are already planned or waiting for approval",
  unsupported: "their format cannot be posted as a Story",
  unavailable: "they are no longer available",
  awaiting_reobservation: "they are waiting to be indexed again",
  destination_not_allowed: "they cannot be used for this account",
};

export function exclusionSentence({ reason, count: n }: { reason: string; count: number }): string {
  const why = Object.hasOwn(EXCLUSION_COPY, reason)
    ? EXCLUSION_COPY[reason]
    : "they cannot be used right now";
  return `${n.toLocaleString("en-US")} ${n === 1 ? "post is" : "posts are"} left out because ${why}.`;
}

/** How an account is named on the overview: `@handle`, else its name. */
export function readinessName(d: Pick<ReadinessDestination, "handle" | "displayName">): string {
  if (d.handle) return `@${d.handle}`;
  return d.displayName ?? "This account";
}

/**
 * The sentence under an account. Indexing is progress, never completion: no
 * percentage, no promise of all history. While it runs, what is left out so
 * far is said as observed, not as a final yield.
 */
export function readinessDetail(d: ReadinessDestination): string {
  const name = readinessName(d);
  const leading = d.leadingExclusion ? exclusionSentence(d.leadingExclusion) : null;
  switch (d.state) {
    case "inactive":
      return `${name} is disabled or moved, so it is left out of setup.`;
    case "needs_connection":
      return `${name} is not connected to Instagram yet, so nothing is posted to it.`;
    case "needs_reconnect":
      return `${name}'s Instagram access needs reconnecting.`;
    case "sources_off":
      return `Every source is Off, so nothing is drawn for ${name}.`;
    case "no_assignable_source":
      return d.remedy === "enable_history"
        ? `${name}'s past posts are Off, and no Google Drive folder is on.`
        : `${name} has nothing to post from yet.`;
    case "indexing":
      return `Indexing ${name}'s past posts. Matching posts appear as indexing continues.${leading ? ` So far, ${leading}` : ""}`;
    case "zero_eligible":
      return `No posts match for ${name} yet.${leading ? ` ${leading}` : ""}`;
    case "ready": {
      const n = d.candidates ?? 0;
      const partial = d.partialCoverage ? " Not every past post is indexed yet." : "";
      return `${n.toLocaleString("en-US")} matching ${n === 1 ? "post" : "posts"}. ${MEDIA_CHECKED_COPY}${partial}`;
    }
    case "stalled":
      return `Indexing ${name}'s past posts has stalled.`;
    case "error":
      return `A source ${name} posts from has stopped working.`;
  }
  return `${name} is in a state this page does not recognise.`;
}

/** A day as coverage is said, "October 1, 2020", on UTC's clock so every reader sees the same one. */
function coverageDate(iso: string): string | null {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  return at.toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  });
}

/** The oldest indexed post, as coverage evidence and nothing more. */
export function coverageLine(d: ReadinessDestination): string | null {
  const date = d.oldestObservedAt ? coverageDate(d.oldestObservedAt) : null;
  return date ? `Oldest post indexed so far: ${date}.` : null;
}

// ── One source's own status, on the Posting mix card ───────────────────────

/** A badge on a source's row: its words, and the tone it is drawn in. */
export type SourceBadge = { label: string; tone: BadgeTone };

/**
 * The tone of each per-source status that is also a destination state, by the
 * server's own tone names (`vocabulary.READINESS_TONES`, held equal by
 * `wire-contract.test.ts`), through `readinessTone`, the closed table the
 * destination badges use. Partial coverage's is `PARTIAL_COVERAGE_BADGE`'s.
 */
const SOURCE_STATUS_TONE = {
  needs_reconnect: "attention",
  stalled: "problem",
  indexing: "progress",
} as const;

/**
 * The badges for an Instagram source as the viewed destination's entry for it
 * sent it (`destinations[].sources[]`: `needs_reconnect`, `stalled`, `indexing`,
 * `partial_coverage`), in the destination badges' own words and tones. Only a
 * flag the API sent as true draws a badge: one it did not send, or sent false,
 * draws nothing, and nothing here works a status out from anything else.
 *
 * One status, then partial coverage. Stalled is indexing that has missed its
 * progress budget (`instagram_sync.status` sends it only while indexing), and a
 * source that needs reconnecting is not progressing until it does, so of those
 * three the one that says most is drawn, as the destination's own state takes
 * the first that matches (reconnect, then stalled, then indexing), never a
 * contradicting pair ("Indexing" beside "Indexing stalled"). Partial coverage
 * is a separate fact, and is drawn beside any of them.
 */
export function sourceStatusBadges(view: DestinationSourceView | null): SourceBadge[] {
  if (!view) return [];
  const status =
    view.needsReconnect === true
      ? "needs_reconnect"
      : view.stalled === true
        ? "stalled"
        : view.indexing === true
          ? "indexing"
          : null;
  const badges: SourceBadge[] = [];
  if (status) {
    badges.push({
      label: readinessStateLabel(status),
      tone: readinessTone(SOURCE_STATUS_TONE[status]),
    });
  }
  if (view.partialCoverage === true) badges.push({ ...PARTIAL_COVERAGE_BADGE });
  return badges;
}

/**
 * "Indexed back to October 1, 2020": the oldest post indexed from this source,
 * as coverage evidence and nothing more. It says how far back the index
 * reaches, never that the history is all there, and nothing like a progress.
 */
export function indexedBackLine(view: DestinationSourceView | null): string | null {
  const date = view?.oldestObservedAt ? coverageDate(view.oldestObservedAt) : null;
  return date ? `Indexed back to ${date}` : null;
}

export type ReadinessLink = { href: string; label: string };

/** An admin's remedy: a link to where it is done, labelled with the account. */
export function remedyLink(d: ReadinessDestination): ReadinessLink | null {
  const name = readinessName(d);
  switch (d.remedy) {
    case "connect":
      return { href: RESOLVED_IN.accounts.href, label: `Connect ${name}` };
    case "reconnect":
      return { href: RESOLVED_IN.accounts.href, label: `Reconnect ${name}` };
    case "enable_history":
      return { href: RESOLVED_IN.mix.href, label: `Turn on ${name}'s past posts` };
    case "add_source":
      return { href: RESOLVED_IN.integrations.href, label: `Add a Drive folder for ${name}` };
    case "review_policy":
      return { href: RESOLVED_IN.mix.href, label: `Review ${name}'s past-posts settings` };
    case "retry":
      return d.driveError
        ? { href: RESOLVED_IN.integrations.href, label: `Check ${name}'s Drive folder` }
        : { href: RESOLVED_IN.mix.href, label: `Check ${name}'s past posts` };
  }
  return null;
}

/** A member's remedy: who can do it, with no button to a page that refuses them. */
export function remedyAsk(d: ReadinessDestination): string | null {
  const name = readinessName(d);
  switch (d.remedy) {
    case "connect":
      return `Ask an admin to connect ${name}.`;
    case "reconnect":
      return `Ask an admin to reconnect ${name}.`;
    case "enable_history":
      return `Ask an admin to enable ${name}'s past posts or add a Drive folder.`;
    case "add_source":
      return `Ask an admin to add a Drive folder for ${name}.`;
    case "review_policy":
      return `Ask an admin to review ${name}'s past-posts settings.`;
    case "retry":
      return `Ask an admin to check the source ${name} posts from.`;
  }
  return null;
}

export type ReadinessLine = {
  key: string;
  name: string;
  badges: { label: string; tone: BadgeTone }[];
  detail: string;
  coverage: string | null;
  action: ReadinessLink | null;
  ask: string | null;
};

export type ReadinessView = {
  /** All accounts inactive, or every source Off: a choice, said as one. */
  intentional: boolean;
  title: string;
  detail: string | null;
  action: ReadinessLink | null;
  lines: ReadinessLine[];
};

function lineFor(d: ReadinessDestination, isAdmin: boolean): ReadinessLine {
  return {
    key: d.accountId,
    name: readinessName(d),
    badges: [
      { label: readinessStateLabel(d.state), tone: d.tone },
      ...(d.partialCoverage ? [PARTIAL_COVERAGE_BADGE] : []),
    ],
    detail: readinessDetail(d),
    coverage: coverageLine(d),
    action: isAdmin ? remedyLink(d) : null,
    ask: isAdmin ? null : remedyAsk(d),
  };
}

/**
 * The overview's guidance, or null when there is nothing to say (every active
 * account ready, none with partial coverage). The accounts still being set up
 * come first, in the server's order; a ready account with partial coverage
 * follows, since that badge never hides its matching posts. An intentional
 * workspace says its choice with an optional control and no setup pressure.
 */
export function readinessView(
  readiness: Readiness,
  { isAdmin }: { isAdmin: boolean },
): ReadinessView | null {
  const { destinations, guidance } = readiness;
  const byId = new Map(destinations.map((d) => [d.accountId, d]));
  const incomplete = guidance.incomplete.flatMap((id) => {
    const d = byId.get(id);
    return d ? [d] : [];
  });
  const listed = new Set(incomplete.map((d) => d.accountId));
  const partial = destinations.filter(
    (d) => !listed.has(d.accountId) && d.state !== "inactive" && d.partialCoverage,
  );
  const lines = [...incomplete, ...partial].map((d) => lineFor(d, isAdmin));
  if (guidance.intentional === "all_inactive") {
    return {
      intentional: true,
      title: "Every account is inactive",
      detail: "Nothing is scheduled while every account is disabled or moved, so there is nothing to set up.",
      action: isAdmin ? { href: RESOLVED_IN.accounts.href, label: RESOLVED_IN.accounts.action } : null,
      lines,
    };
  }
  if (guidance.intentional === "all_sources_off") {
    return {
      intentional: true,
      title: "Every source is Off",
      detail: "Nothing is drawn while every source is Off. Turn one on in Posting mix whenever you want posts again.",
      action: isAdmin ? { href: RESOLVED_IN.mix.href, label: RESOLVED_IN.mix.action } : null,
      lines,
    };
  }
  if (lines.length === 0) return null;
  return {
    intentional: false,
    title: `${guidance.ready} of ${guidance.active} ${guidance.active === 1 ? "account" : "accounts"} ready`,
    detail: null,
    action: null,
    lines,
  };
}

/**
 * The overview's setup guidance — THE SWITCH between the two protocols, made
 * in one place. *readiness* is `parseReadiness`'s answer for the mix read.
 * Where there is one (protocol 2): with no account to post to, the first step
 * is the one both protocols share (connect one); otherwise the readiness
 * view, and no row-existence step, since a Drive folder is optional where an
 * account's own past posts serve. Where there is none (protocol 1, or an API
 * that sends none), `nextSetupStep`.
 */
export function overviewSetup(
  readiness: Readiness | null,
  progress: SetupProgress,
  { isAdmin }: { isAdmin: boolean },
): { setupStep: SetupStep | null; readiness: ReadinessView | null } {
  if (readiness === null) return { setupStep: nextSetupStep(progress, { isAdmin }), readiness: null };
  if (readiness.destinations.length === 0) {
    return { setupStep: nextSetupStep({ accounts: [], sources: [] }, { isAdmin }), readiness: null };
  }
  return { setupStep: null, readiness: readinessView(readiness, { isAdmin }) };
}

/**
 * Why an account's runway is what it is, for every account that is not ready
 * or inactive: the readiness sentence beside its days of content left.
 */
export function runwayNotes(readiness: Readiness | null): Record<string, string> {
  const notes: Record<string, string> = {};
  for (const d of readiness?.destinations ?? []) {
    if (d.state !== "ready" && d.state !== "inactive") notes[d.accountId] = readinessDetail(d);
  }
  return notes;
}
