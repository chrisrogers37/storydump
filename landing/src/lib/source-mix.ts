import { callBff, postJson } from "./bff";

/**
 * The posting mix of a CONVERTED workspace: protocol 2.
 *
 * Protocol 1 (`category-mix.ts`) weighs Drive folders as explicit, Automatic
 * or Off, and its card keeps doing that for every workspace that has not
 * converted. GET says which protocol a workspace speaks (`protocol_version`);
 * nothing here guesses it from the rows. A converted workspace has two modes,
 * Equal shares (100% over the sources that are on) and Custom percentages, and
 * three actions. Each action carries the revision it was made against, so a
 * stale one is refused (`mix_conflict`, 409) instead of landing over someone
 * else's save.
 *
 * SHARES ARE DECIMAL STRINGS, AND STAY STRINGS ON THE WAY BACK. The API keeps
 * ratios as unscaled NUMERIC, so a third arrives as fifty digits. A share read
 * into a float and written back would not be the share that was read. Display
 * reads the digits off the string (`largestRemainder`), and a custom save is
 * written from integer basis points (`ratioText`). No `parseFloat` sits between
 * a value read and a value written.
 */

export type MixMode = "equal" | "custom";

export type SourceMixRow = {
  sourceId: string;
  /** `instagram_archive`, `gdrive`, or a provider this page has no words for. */
  provider: string;
  /** The folder name for Drive. An Instagram row is titled "Your Instagram". */
  name: string;
  state: string;
  enabled: boolean;
  /** The stored ratio as sent; `"0"` is Off. */
  ratio: string;
  /** This source's share of the workspace target, as sent; `"0"` when Off. */
  targetShare: string;
  /** The account an Instagram source belongs to, without the `@`. */
  originHandle: string | null;
  /** Retained items, any provider. Null from an API that does not send it. */
  mediaCount: number | null;
  /** Current-generation observations, Instagram only. Null without evidence. */
  indexed: number | null;
};

/**
 * One source as one destination sees it: read-only.
 *
 * The four progress flags and `oldestObservedAt` are an archive's own facts
 * (`instagram_sync.status`), sent beside every destination's entry for it, and
 * stay what the API sent: a flag it did not send, or sent as anything but a
 * boolean, is null and says nothing, never false. A Drive folder has none.
 */
export type DestinationSourceView = {
  sourceId: string;
  /** False: this source cannot serve this account. Null: the API did not say. */
  assignable: boolean | null;
  /** Metadata-matching posts for this account (`candidates`), not verified media. */
  candidates: number | null;
  /** This account's effective share, as sent. */
  effectiveShare: string | null;
  /** The archive is still working through its history. */
  indexing: boolean | null;
  /** Not every past post is indexed yet. */
  partialCoverage: boolean | null;
  /** Indexing, and past the budget it had to show progress. */
  stalled: boolean | null;
  /** Its origin's grant is unusable, or its sync failed. */
  needsReconnect: boolean | null;
  /** The oldest post indexed from it (a time): coverage evidence, never a promise of all history. */
  oldestObservedAt: string | null;
};

export type MixDestination = {
  accountId: string;
  handle: string | null;
  displayName: string | null;
  sources: DestinationSourceView[];
};

export type SourceMix = {
  revision: number;
  mode: MixMode;
  /** The actions the API takes for this mix; a control without one is not drawn. */
  supportedActions: string[];
  /** `conversion.kind`: how this workspace's mix came to protocol 2. */
  conversionKind: string | null;
  rows: SourceMixRow[];
  destinations: MixDestination[];
  /** `reset_explanation` on a mutation's answer; null on a read. */
  resetExplanation: string | null;
  /** Whether past posts can be used here at all (`archive_available`). */
  archiveAvailable: boolean;
};

// ── Reading the resource ────────────────────────────────────────────────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A share or ratio as the API writes it: digits, an optional point, digits. */
export function isDecimalString(value: unknown): value is string {
  return typeof value === "string" && /^\d+(\.\d+)?$/.test(value);
}

function count(value: unknown): number | null {
  return Number.isSafeInteger(value) && (value as number) >= 0 ? (value as number) : null;
}

/** A handle with any `@` the API or a person left on it taken off; null when blank. */
export function bareHandle(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const handle = value.trim().replace(/^@+/, "").trim();
  return handle ? handle : null;
}

function readRow(raw: unknown): SourceMixRow | null {
  if (!isRecord(raw)) return null;
  const { source_id, provider, enabled, ratio, target_share } = raw;
  if (
    typeof source_id !== "string" ||
    source_id === "" ||
    typeof provider !== "string" ||
    typeof enabled !== "boolean" ||
    !isDecimalString(ratio) ||
    !isDecimalString(target_share)
  ) {
    return null;
  }
  return {
    sourceId: source_id,
    provider,
    name: typeof raw.name === "string" ? raw.name : "",
    state: typeof raw.state === "string" ? raw.state : "",
    enabled,
    ratio,
    targetShare: target_share,
    originHandle: bareHandle(raw.origin_handle),
    mediaCount: count(raw.media_count),
    indexed: count(raw.indexed),
  };
}

/** A flag as the API sent it, or null when it sent none that is a boolean. */
function flag(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

/** A time as the API sent it, or null when it is not one that can be read. */
function observedAt(value: unknown): string | null {
  return typeof value === "string" && !Number.isNaN(Date.parse(value)) ? value : null;
}

function readDestination(raw: unknown): MixDestination | null {
  if (!isRecord(raw) || typeof raw.account_id !== "string") return null;
  const sources = Array.isArray(raw.sources) ? raw.sources : [];
  return {
    accountId: raw.account_id,
    handle: bareHandle(raw.handle),
    displayName:
      typeof raw.display_name === "string" && raw.display_name.trim()
        ? raw.display_name
        : null,
    sources: sources.flatMap((s) =>
      isRecord(s) && typeof s.source_id === "string"
        ? [
            {
              sourceId: s.source_id,
              assignable: typeof s.assignable === "boolean" ? s.assignable : null,
              candidates: count(s.candidates),
              effectiveShare: isDecimalString(s.effective_share) ? s.effective_share : null,
              indexing: flag(s.indexing),
              partialCoverage: flag(s.partial_coverage),
              stalled: flag(s.stalled),
              needsReconnect: flag(s.needs_reconnect),
              oldestObservedAt: observedAt(s.oldest_observed_at),
            },
          ]
        : [],
    ),
  };
}

/**
 * A protocol-2 answer as the card's model, or null when it is not one.
 *
 * STRICT ABOUT WHAT A WRITE IS BUILT FROM. A row whose id, enablement, ratio
 * or share cannot be read makes the whole answer unreadable: a custom save
 * lists every source, and one dropped row is a row the save would silently
 * leave out (the API refuses that as `invalid_mix_source_set`, but the card
 * should not offer it). What only labels or counts a row is optional, so an
 * answer without it still draws a card.
 */
export function parseSourceMix(data: unknown): SourceMix | null {
  if (!isRecord(data) || data.protocol_version !== 2) return null;
  const { revision, mode } = data;
  if (!Number.isSafeInteger(revision) || (revision as number) < 0) return null;
  if (mode !== "equal" && mode !== "custom") return null;
  if (!Array.isArray(data.rows)) return null;
  const rows = data.rows.map(readRow);
  if (rows.some((row) => row === null)) return null;
  const destinations = Array.isArray(data.destinations) ? data.destinations : [];
  return {
    revision: revision as number,
    mode,
    supportedActions: Array.isArray(data.supported_actions)
      ? data.supported_actions.filter((a): a is string => typeof a === "string")
      : [],
    conversionKind:
      isRecord(data.conversion) && typeof data.conversion.kind === "string"
        ? data.conversion.kind
        : null,
    rows: rows as SourceMixRow[],
    destinations: destinations.flatMap((d) => {
      const read = readDestination(d);
      return read ? [read] : [];
    }),
    resetExplanation:
      typeof data.reset_explanation === "string" ? data.reset_explanation : null,
    archiveAvailable: data.archive_available === true,
  };
}

/**
 * The newer of two readings, by revision; the first on a tie.
 *
 * The page's read and the card's own last answer can disagree for a moment:
 * a save answers revision 9 before `router.refresh()` brings the page's read
 * to 9, and another admin's save can bring the page to 10 first. The revision
 * only grows, so the higher one is the current mix. On a tie the page's read
 * wins, since its counts are the fresher.
 */
export function newerMix(first: SourceMix | null, second: SourceMix | null): SourceMix | null {
  if (!first) return second;
  if (!second) return first;
  return second.revision > first.revision ? second : first;
}

// ── Exact display ───────────────────────────────────────────────────────────

/** `value × 10^places`, split at the point: whole units, then the digits after. */
function scaled(value: string, places: number): { units: number; rest: string } {
  const [whole, fraction = ""] = value.split(".");
  const digits = fraction.padEnd(places, "0");
  return {
    units: Number(whole + digits.slice(0, places)),
    rest: digits.slice(places).replace(/0+$/, ""),
  };
}

/** Two runs of digits after a point, compared as the fractions they are. */
function compareFractions(a: string, b: string): number {
  const width = Math.max(a.length, b.length);
  const x = a.padEnd(width, "0");
  const y = b.padEnd(width, "0");
  return x < y ? -1 : x > y ? 1 : 0;
}

/**
 * Each decimal share in whole units of `10^-places`, adding to what the shares
 * add to. Each is rounded down, and the units that leaves over go to the
 * largest remainders, the earlier row on a tie. Read off the digits, so fifty
 * of them compare exactly. A mix's shares add to 1 (or 0 when every source is
 * Off), so the units add to `10^places` (or 0).
 */
export function largestRemainder(shares: readonly string[], places: number): number[] {
  const parts = shares.map((share) => scaled(isDecimalString(share) ? share : "0", places));
  const units = parts.map((part) => part.units);
  // The remainders add to a whole number of units when the shares do; the
  // float sum is only rounded to find which whole number.
  let left = Math.round(parts.reduce((sum, part) => sum + Number(`0.${part.rest || "0"}`), 0));
  const order = parts
    .map((part, i) => ({ i, rest: part.rest }))
    .sort((a, b) => compareFractions(b.rest, a.rest) || a.i - b.i);
  for (const { i } of order) {
    if (left <= 0) break;
    units[i] += 1;
    left -= 1;
  }
  return units;
}

/**
 * Shares as percents to one decimal that add to 100 (0 when every source is
 * Off): 50 and 50, 33.4, 33.3 and 33.3, a custom 12.5 shown as 12.5.
 */
export function sharePercents(shares: readonly string[]): number[] {
  return largestRemainder(shares, 3).map((tenths) => tenths / 10);
}

export function percentLabel(percent: number): string {
  return `${percent}%`;
}

/**
 * A row's target as shown: Off, or its percent. A source that is on and has a
 * share too small to show is "<0.1%", never "0%": rounding for display must
 * not make a source that is on read as Off.
 */
export function targetLabel(row: Pick<SourceMixRow, "enabled">, percent: number): string {
  if (!row.enabled) return "Off";
  return percent === 0 ? "<0.1%" : percentLabel(percent);
}

// ── Labels and copy ─────────────────────────────────────────────────────────

const PROVIDER_LABEL: Record<string, string> = {
  instagram_archive: "Instagram",
  gdrive: "Google Drive",
};

export function isInstagram(row: Pick<SourceMixRow, "provider">): boolean {
  return row.provider === "instagram_archive";
}

/** The row's title: "Your Instagram", or the folder's name. */
export function sourceTitle(row: SourceMixRow): string {
  if (isInstagram(row)) return "Your Instagram";
  return row.name.trim() || "Unnamed source";
}

/** The line under the title: "@<handle> · Instagram", or "Google Drive". */
export function sourceSubtitle(row: SourceMixRow): string {
  const provider = PROVIDER_LABEL[row.provider] ?? row.provider;
  if (isInstagram(row) && row.originHandle) return `@${row.originHandle} · ${provider}`;
  return provider;
}

/** What a control on this row is called: the title AND the handle or provider. */
export function sourceAccessibleName(row: SourceMixRow): string {
  if (isInstagram(row)) {
    return row.originHandle ? `Your Instagram @${row.originHandle}` : "Your Instagram";
  }
  return `${sourceTitle(row)} (${PROVIDER_LABEL[row.provider] ?? row.provider})`;
}

/**
 * The sources that are on, each with its share, as one line: "Your Instagram
 * @handle 50%, Trips (Google Drive) 50%". Null when none is on. The shares are
 * the mix's own, as sent, so this says an applied mix and a previewed one alike.
 */
export function enabledSharesLine(mix: Pick<SourceMix, "rows">): string | null {
  const percents = sharePercents(mix.rows.map((row) => row.targetShare));
  const on = mix.rows
    .map((row, i) => ({ row, percent: percents[i] }))
    .filter(({ row }) => row.enabled);
  if (on.length === 0) return null;
  return on
    .map(({ row, percent }) => `${sourceAccessibleName(row)} ${targetLabel(row, percent)}`)
    .join(", ");
}

/**
 * What Off does, per provider. Never "delete": Off stops future use, and what
 * was indexed or synced stays.
 */
export function offCopy(provider: string): string {
  switch (provider) {
    case "instagram_archive":
      return "Stops indexing and selecting past posts; indexed metadata stays until deletion.";
    case "gdrive":
      return "Stops selecting this folder; syncing continues.";
  }
  return "Stops selecting this source.";
}

export const MEMBERSHIP_RESET_COPY =
  "Changing which sources are on resets every share to equal.";
export const LONG_RUN_COPY =
  "Shares are long-run targets, not exact quotas over a few posts.";
export const MEDIA_CHECKED_COPY = "Media is checked when used.";

export const MODE_COPY: Record<MixMode, { label: string; detail: string }> = {
  equal: {
    label: "Equal shares",
    detail: "Every source that is on gets the same share, whatever its size.",
  },
  custom: {
    label: "Custom percentages",
    detail: "Each source that is on gets the percentage set for it.",
  },
};

/**
 * The one-time conversion, said as what it did, and only while
 * it still describes the mix. The read keeps the conversion's kind for good, but
 * a person who saves custom percentages over Equal shares, or resets Custom to
 * equal, has a mix the sentence no longer says: "became Equal shares: every
 * source that is on now gets the same share" above "Custom percentages 70/30"
 * is a statement the card contradicts. So Automatic becoming Equal is said while
 * the mode is Equal, and a custom mix kept or translated while it is Custom.
 *
 * Nothing for a mix that started on protocol 2 (`new_equal`) or whose record
 * says nothing (`legacy_intent`). The hybrid sentence claims no exact match with
 * what Automatic drew: it was translated from item counts once, and then fixed.
 */
export function conversionNotice(kind: string | null, mode: MixMode): string | null {
  switch (kind) {
    case "automatic_to_equal":
      return mode === "equal"
        ? "Automatic shares, which followed file counts, became Equal shares: every source that is on now gets the same share."
        : null;
    case "explicit_preserved":
      return mode === "custom" ? "Your custom percentages were kept as you set them." : null;
    case "hybrid_to_custom":
      return mode === "custom"
        ? "Your percentages and Automatic folders were translated once into fixed custom percentages, using how many items each source held at the time. They no longer follow file counts."
        : null;
  }
  return null;
}

/** The answer's `reset_explanation`, as a sentence. */
export function resetExplanationCopy(explanation: string | null): string | null {
  switch (explanation) {
    case "enabled_membership_changed":
      return "Which sources are on changed, so every share was reset to equal.";
    case "equal_enabled_sources":
      return "Every source that is on now has an equal share.";
  }
  return null;
}

function counted(n: number, one: string, many: string): string {
  return `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
}

/** How the account is named on screen: `@handle`, else its name. */
export function destinationLabel(destination: MixDestination): string {
  if (destination.handle) return `@${destination.handle}`;
  return destination.displayName ?? "this account";
}

/** A share without its trailing zeros, so "0.50" and "0.5" are one share. */
function trimmedShare(share: string): string {
  return share.includes(".") ? share.replace(/0+$/, "").replace(/\.$/, "") : share;
}

/**
 * Whether the listed accounts see different mixes: more than one, and either
 * not all served by the same sources or not all given the same effective
 * shares.
 *
 * An account's scope is the sources the API says can serve it (`assignable`).
 * A Drive folder serves every account and an archive only its own, so a
 * workspace of Drive folders alone has one mix whoever is looking, and needs no
 * selector; one whose archive serves one account and not another does. A source
 * the API did not say is assignable is not counted as serving.
 *
 * The same scope can still draw differently. Each account's shares renormalize
 * over the sources that have posts it can use (`category_mix.pool` weighs
 * a source with none at 0), so a folder with nothing usable for one account is
 * out of that account's mix and not another's. The API says it in each
 * account's `effective_share`, and a difference there tells the accounts apart
 * too: hiding it would show one account's mix as the workspace's. Shares are
 * compared only where the API sent both.
 *
 * One predicate for everything that tells the accounts apart: the View as
 * selector, the Workspace targets labels, the preview's account by account
 * lines, and the overview's "each account's own share can differ".
 */
export function destinationsDiffer(destinations: readonly MixDestination[]): boolean {
  if (destinations.length < 2) return false;
  const scopes = destinations.map((d) =>
    d.sources
      .filter((s) => s.assignable === true)
      .map((s) => s.sourceId)
      .sort()
      .join("|"),
  );
  if (new Set(scopes).size > 1) return true;
  const [first, ...others] = destinations;
  return others.some((other) =>
    first.sources.some((a) => {
      const b = other.sources.find((s) => s.sourceId === a.sourceId);
      return (
        a.effectiveShare !== null &&
        b !== undefined &&
        b.effectiveShare !== null &&
        trimmedShare(a.effectiveShare) !== trimmedShare(b.effectiveShare)
      );
    }),
  );
}

/** The row as the viewed destination sees it, or null: no account to view, or none that lists it. */
export function viewedSource(
  row: Pick<SourceMixRow, "sourceId">,
  viewed: MixDestination | null,
): DestinationSourceView | null {
  return viewed?.sources.find((s) => s.sourceId === row.sourceId) ?? null;
}

/**
 * The counts under a row: "124 matching posts · 680 indexed" for Instagram,
 * "12 matching posts · 40 files" for Drive. Matching posts are the viewed
 * account's metadata candidates, never verified media; with no account to
 * view there are none to name. A source that cannot serve the viewed account
 * says so instead.
 */
export function countsLine(row: SourceMixRow, viewed: MixDestination | null): string {
  const seen = viewedSource(row, viewed);
  if (viewed && seen?.assignable === false) {
    return `Not used for ${destinationLabel(viewed)}`;
  }
  const parts: string[] = [];
  if (seen && seen.candidates !== null) {
    parts.push(counted(seen.candidates, isInstagram(row) ? "matching item" : "matching post", isInstagram(row) ? "matching items" : "matching posts"));
  }
  if (isInstagram(row)) {
    if (row.indexed !== null) parts.push(counted(row.indexed, "post indexed", "posts indexed"));
  } else if (row.mediaCount !== null) {
    parts.push(counted(row.mediaCount, "file", "files"));
  }
  return parts.join(" · ");
}

/** The viewed account's effective share of each row, read-only, in percent. */
export function effectivePercents(rows: readonly SourceMixRow[], viewed: MixDestination): number[] {
  return sharePercents(
    rows.map(
      (row) => viewed.sources.find((s) => s.sourceId === row.sourceId)?.effectiveShare ?? "0",
    ),
  );
}

// ── The custom draft ────────────────────────────────────────────────────────

/** A row as the draft holds it: the row it was opened against, and the typed text. */
export type DraftRow = { row: SourceMixRow; percent: string };

/**
 * Custom percentages being edited, against the revision they were opened at.
 * Off rows stay `"0"` and are not edited here: a source goes on or off only
 * through its switch, which previews the equal reset that change makes.
 */
export type CustomDraft = { basedOn: number; rows: DraftRow[] };

/** The API's sum tolerance (`versioned_mix.decimal_rows`): 0.1 points, in basis points. */
export const SUM_TOLERANCE_BP = 10;
const FULL_BP = 10000;

/** A typed percentage in basis points (`"33.33"` → 3333), or null when it is not one. */
export function readPercent(text: string): number | null {
  const m = /^(\d{0,3})(?:\.(\d{0,2}))?$/.exec(text.trim());
  if (!m || (m[1] === "" && !m[2])) return null;
  return Number(m[1] || "0") * 100 + Number((m[2] ?? "").padEnd(2, "0"));
}

/** Basis points as the percentage a person reads and types: 3333 → "33.33", 5000 → "50". */
export function percentText(bp: number): string {
  const whole = Math.floor(bp / 100);
  const fraction = bp % 100;
  if (fraction === 0) return String(whole);
  return `${whole}.${String(fraction).padStart(2, "0").replace(/0$/, "")}`;
}

/** Basis points as the ratio a save carries: 3333 → "0.3333", 10000 → "1". */
export function ratioText(bp: number): string {
  if (bp >= FULL_BP) return "1";
  if (bp <= 0) return "0";
  return `0.${String(bp).padStart(4, "0")}`.replace(/0+$/, "");
}

/**
 * Each source's target share in basis points, adding to exactly 10000 (thirds
 * open as 3334, 3333 and 3333). A source that is on never opens at 0: its
 * share is positive however small, and 0 would read as Off, so it opens at
 * one basis point, taken from the largest share.
 */
function seedBasisPoints(rows: readonly SourceMixRow[]): number[] {
  const bp = largestRemainder(
    rows.map((row) => (row.enabled ? row.targetShare : "0")),
    4,
  );
  rows.forEach((row, i) => {
    if (!row.enabled || bp[i] > 0) return;
    const largest = bp.indexOf(Math.max(...bp));
    if (bp[largest] > 1) {
      bp[largest] -= 1;
      bp[i] = 1;
    }
  });
  return bp;
}

/** A draft opened on the current mix: each source that is on at its target share. */
export function openDraft(mix: SourceMix): CustomDraft {
  const seed = seedBasisPoints(mix.rows);
  return {
    basedOn: mix.revision,
    rows: mix.rows.map((row, i) => ({ row, percent: row.enabled ? percentText(seed[i]) : "0" })),
  };
}

export function editDraft(draft: CustomDraft, sourceId: string, text: string): CustomDraft {
  return {
    ...draft,
    rows: draft.rows.map((r) =>
      r.row.sourceId === sourceId && r.row.enabled ? { ...r, percent: text } : r,
    ),
  };
}

export type MixRatio = { source_id: string; ratio: string };

export type DraftCheck =
  | { ok: true; rows: MixRatio[]; totalBp: number }
  | { ok: false; error: "bad_percent" | "zero_enabled"; sourceId: string; totalBp: null }
  | { ok: false; error: "sum_not_100"; totalBp: number };

/**
 * Whether a draft can be saved, and the rows it saves. A source that is on
 * needs a percentage above 0 (setting one to 0 would switch it off, which is
 * the switch's change, not this save's) and at most 100, with at most two
 * decimals. The percentages must add up to 100 within the API's tolerance;
 * the API puts the last fraction on the largest share.
 */
export function checkDraft(draft: CustomDraft): DraftCheck {
  const rows: MixRatio[] = [];
  let totalBp = 0;
  for (const { row, percent } of draft.rows) {
    if (!row.enabled) {
      rows.push({ source_id: row.sourceId, ratio: "0" });
      continue;
    }
    const bp = readPercent(percent);
    if (bp === null || bp > FULL_BP) {
      return { ok: false, error: "bad_percent", sourceId: row.sourceId, totalBp: null };
    }
    if (bp === 0) {
      return { ok: false, error: "zero_enabled", sourceId: row.sourceId, totalBp: null };
    }
    totalBp += bp;
    rows.push({ source_id: row.sourceId, ratio: ratioText(bp) });
  }
  const anyOn = draft.rows.some((r) => r.row.enabled);
  if (anyOn && Math.abs(totalBp - FULL_BP) > SUM_TOLERANCE_BP) {
    return { ok: false, error: "sum_not_100", totalBp };
  }
  return { ok: true, rows, totalBp };
}

export type DraftRowChange = "kept" | "added" | "removed" | "turned_on" | "turned_off";

export type DraftComparisonRow = {
  /** The row as it is now; the draft's copy for a row that is gone. */
  row: SourceMixRow;
  change: DraftRowChange;
  /** What the kept draft says for it; null for a row the draft never had. */
  draftPercent: string | null;
  /** Whether the row was on when the draft was opened; null for a row it never had. */
  draftEnabled: boolean | null;
  /** Its saved target share now; null for a row that is gone. */
  savedShare: string | null;
};

/**
 * A kept draft beside the mix as it is now, after a conflict: every row
 * either side has, marked by what changed. Rows the draft had and the mix no
 * longer does are listed last, as removed, never silently dropped.
 */
export function compareDraft(draft: CustomDraft, mix: SourceMix): DraftComparisonRow[] {
  const kept = new Map(draft.rows.map((r) => [r.row.sourceId, r]));
  const now: DraftComparisonRow[] = mix.rows.map((row) => {
    const was = kept.get(row.sourceId);
    const change: DraftRowChange = !was
      ? "added"
      : was.row.enabled === row.enabled
        ? "kept"
        : row.enabled
          ? "turned_on"
          : "turned_off";
    return {
      row,
      change,
      draftPercent: was ? was.percent : null,
      draftEnabled: was ? was.row.enabled : null,
      savedShare: row.targetShare,
    };
  });
  const present = new Set(mix.rows.map((row) => row.sourceId));
  const gone: DraftComparisonRow[] = draft.rows
    .filter((r) => !present.has(r.row.sourceId))
    .map((r) => ({
      row: r.row,
      change: "removed",
      draftPercent: r.percent,
      draftEnabled: r.row.enabled,
      savedShare: null,
    }));
  return [...now, ...gone];
}

/**
 * The kept draft carried onto the mix as it is now, by the person's choice:
 * a source still on keeps the typed percentage; one that came on (or is new)
 * starts at its current target; one that went off is `"0"`; a removed one
 * leaves. The result is a draft like any other, saved only by an explicit
 * save, and only if it adds up.
 */
export function rebaseDraft(draft: CustomDraft, mix: SourceMix): CustomDraft {
  const kept = new Map(draft.rows.map((r) => [r.row.sourceId, r]));
  const fresh = openDraft(mix);
  return {
    basedOn: mix.revision,
    rows: fresh.rows.map((r) => {
      const was = kept.get(r.row.sourceId);
      return r.row.enabled && was?.row.enabled ? { row: r.row, percent: was.percent } : r;
    }),
  };
}

// ── The actions and the two doors ───────────────────────────────────────────

export type ResetEqualAction = {
  protocol_version: 2;
  action: "reset_equal";
  expected_revision: number;
};
export type SetEnabledAction = {
  protocol_version: 2;
  action: "set_enabled";
  source_id: string;
  enabled: boolean;
  expected_revision: number;
};
export type SaveCustomAction = {
  protocol_version: 2;
  action: "save_custom";
  expected_revision: number;
  rows: MixRatio[];
};
export type MixAction = ResetEqualAction | SetEnabledAction | SaveCustomAction;
/** The two actions that reset to equal, and so are previewed before they are made. */
export type ChangeAction = ResetEqualAction | SetEnabledAction;

export function resetEqualAction(revision: number): ResetEqualAction {
  return { protocol_version: 2, action: "reset_equal", expected_revision: revision };
}

export function setEnabledAction(
  revision: number,
  sourceId: string,
  enabled: boolean,
): SetEnabledAction {
  return {
    protocol_version: 2,
    action: "set_enabled",
    source_id: sourceId,
    enabled,
    expected_revision: revision,
  };
}

/** A custom save: every current source, Off rows as `"0"`, against the draft's revision. */
export function saveCustomAction(revision: number, rows: MixRatio[]): SaveCustomAction {
  return { protocol_version: 2, action: "save_custom", expected_revision: revision, rows };
}

/**
 * The two fields a write to a converted workspace's SOURCES carries, as the
 * mix's own actions do: the protocol it speaks and the revision it was made
 * against (`versioned_mix.connect_drive` and `remove_drive` refuse a converted
 * workspace without both, `mix_protocol_required` and `mix_conflict`). A
 * workspace on protocol 1 has no revision to carry: pass null, and the write
 * carries neither, since the API refuses protocol 2's fields on a workspace
 * that has not converted.
 */
export function versionFields(revision: number | null): {
  protocol_version?: 2;
  expected_revision?: number;
} {
  return revision === null ? {} : { protocol_version: 2, expected_revision: revision };
}

export type MixCallResult =
  | { ok: true; mix: SourceMix }
  | { ok: false; error: string; status: number; currentRevision: number | null };

async function call(path: string, action: MixAction, method: "PUT" | "POST"): Promise<MixCallResult> {
  const result = await callBff(path, postJson(action, method));
  if (!result.ok) {
    const revision = result.body.current_revision;
    return {
      ok: false,
      error: result.error,
      status: result.status,
      currentRevision:
        Number.isSafeInteger(revision) && (revision as number) >= 0 ? (revision as number) : null,
    };
  }
  const mix = parseSourceMix(result.data);
  return mix
    ? { ok: true, mix }
    : { ok: false, error: "malformed_response", status: result.status, currentRevision: null };
}

/** Make the change: the mix resource's PUT, which answers the whole new mix. */
export function putSourceMix(workspaceId: string, action: MixAction): Promise<MixCallResult> {
  return call(`/api/workspaces/${workspaceId}/category-mix`, action, "PUT");
}

/**
 * Ask what the change would make, without making it: the same body, the
 * would-be mix in the answer, and the revision unchanged. A stale revision is
 * refused here exactly as it would be by the PUT.
 */
export function previewSourceMix(workspaceId: string, action: ChangeAction): Promise<MixCallResult> {
  return call(`/api/workspaces/${workspaceId}/category-mix/preview`, action, "POST");
}
