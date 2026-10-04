/**
 * The dashboard's badge vocabulary: five tones and their Tailwind.
 *
 * SEMANTICS AND CLASSES ARE STILL SPLIT, and that split is the reason this
 * file is in `components/` rather than `lib/`. `lib/destination.ts`,
 * `lib/drive.ts` and `api-tokens-tab.tsx` each decide which tone a state
 * DESERVES — a pure question, unit-tested without a DOM, and each keeps its
 * own `Record` keyed on its own closed state set so a state without a badge
 * stays a compile error. What they no longer each own is the tone union
 * itself and the tone→class map, which are one visual language and were
 * written out three times and two-and-a-half times respectively.
 *
 * THE INVARIANT THE COPIES EACH RESTATED: only `active` is ever green.
 * `attention` is amber because something is asking to be looked at; `inert`
 * is the muted pair rather than a third colour, because `disabled` and
 * `moved` (and `revoked` and `expired`) differ in their LABEL, not in kind,
 * and inventing a colour per state would claim otherwise.
 *
 * The two `lib/` importers take it `import type`, which is erased at compile
 * time and creates no runtime edge from `lib/` to `components/`. That is the
 * one direction exception and it is deliberate: putting the union in `lib/`
 * would put a Tailwind decision in the layer whose whole docblock argument is
 * that it carries no Tailwind.
 */
import type { IntentState } from "@/lib/intents";

export type BadgeTone = "active" | "attention" | "inert" | "progress" | "problem";

export const TONE_CLASS: Record<BadgeTone, string> = {
  active: "bg-green-100 text-green-800",
  attention: "bg-amber-100 text-amber-900",
  inert: "bg-muted text-muted-foreground",
  /** Under way (approved, publishing, queued): the site's tap orange. */
  progress: "bg-tap/15 text-tap-ink",
  /** Gone wrong and needs a person: the site's error red. */
  problem: "bg-alarm/10 text-alarm",
};

/** A legend dot in each tone's colour. */
export const TONE_DOT: Record<BadgeTone, string> = {
  active: "bg-green-500",
  attention: "bg-amber-500",
  inert: "bg-muted-foreground",
  progress: "bg-tap",
  problem: "bg-alarm",
};

/** Each Story state's badge tone, for the Queue and Recent activity. A full
 *  Record, so a new state without a tone is a compile error. */
export const INTENT_STATE_TONE: Record<IntentState, BadgeTone> = {
  scheduled: "inert",
  prompt_pending: "inert",
  awaiting_approval: "attention",
  approved: "progress",
  publishing: "progress",
  publishing_ambiguous: "problem",
  review_required: "problem",
  posted: "active",
  skipped: "attention",
  rejected: "problem",
  expired: "inert",
  failed: "problem",
  cancelled: "inert",
};
