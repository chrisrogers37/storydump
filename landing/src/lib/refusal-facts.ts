/**
 * A port refusal's FACTS: the one structured part of a refusal that may cross
 * to the browser (#1413 phase 6).
 *
 * The command port refuses with `{detail, reason, facts?}` (`src/api/app.py`,
 * `_command_body`). `readError` keeps only the reason, on purpose: a failed
 * call's body is where a token or a subject ends up if anything upstream is
 * careless, and that value reaches logs and error pages. `detail` is a sentence
 * and stays behind. `facts` is what a person needs to act on a refusal: whether
 * a lock can be overridden and what is in the way, which rule a planned time
 * broke, which of two things was not found, and whether the story already
 * holding an item was cancelled a moment ago.
 *
 * So facts cross as an ALLOW-LIST, never as a filter over whatever arrived: the
 * keys below, each with its one type, and nothing else. A code is the shape
 * `readError` admits for a reason; the one nested object is `existing`, with
 * its own three keys. No string that is not a code passes, so no sentence,
 * token, path or id can ride a fact. An unlisted key is dropped, and so is a
 * listed one whose value has the wrong type; facts with nothing left are none.
 *
 * Only the command route asks for them (`targetFetch`'s `refusalFacts`), and
 * the browser re-checks what it receives with this same function.
 */

/** A refusal reason, or a fact's text: lower-case letters, digits and `_`, 1 to 64 of them. */
export function isPlainCode(value: unknown): value is string {
  return typeof value === "string" && /^[a-z0-9_]{1,64}$/.test(value);
}

export type RefusalFacts = {
  /** `locked`: whether `override_locks` gets past what is in the way. */
  overridable?: boolean;
  /** `locked`: the lock kinds in the way, or `item_<state>` for the item itself. */
  in_the_way?: string[];
  /** `invalid_args` on a planned time: the rule it broke. */
  at_rule?: string;
  /** `not_found`: which of the two was not found. */
  missing?: string;
  /** `illegal_transition` on a duplicate: the story that already holds the item. */
  existing?: { state?: string; origin?: string; cancel_requested?: boolean };
};

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function existingFacts(value: unknown): RefusalFacts["existing"] | null {
  if (!isPlainObject(value)) return null;
  const existing: NonNullable<RefusalFacts["existing"]> = {};
  if (isPlainCode(value.state)) existing.state = value.state;
  if (isPlainCode(value.origin)) existing.origin = value.origin;
  if (typeof value.cancel_requested === "boolean") {
    existing.cancel_requested = value.cancel_requested;
  }
  return Object.keys(existing).length > 0 ? existing : null;
}

/** The facts the allow-list admits from `value`, or null when none survive. */
export function refusalFacts(value: unknown): RefusalFacts | null {
  if (!isPlainObject(value)) return null;
  const facts: RefusalFacts = {};
  if (typeof value.overridable === "boolean") facts.overridable = value.overridable;
  // All codes or none: a partial list would misstate what is in the way.
  const inTheWay = value.in_the_way;
  if (Array.isArray(inTheWay) && inTheWay.every(isPlainCode)) {
    facts.in_the_way = [...inTheWay];
  }
  if (isPlainCode(value.at_rule)) facts.at_rule = value.at_rule;
  if (isPlainCode(value.missing)) facts.missing = value.missing;
  const existing = existingFacts(value.existing);
  if (existing) facts.existing = existing;
  return Object.keys(facts).length > 0 ? facts : null;
}
