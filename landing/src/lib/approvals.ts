/**
 * Auto-approval on the web: the approval positions, the rules that
 * approve a story, and the one spelling of every name a person reads for them.
 *
 * MIRRORED, NOT INVENTED. `src/services/target/vocabulary.py` is the API's one
 * spelling of the positions (`APPROVAL_POSITIONS`, approval rules' two CHECKs), the
 * rules (`APPROVAL_RULES`, `ck_intent_approval_rule`) and the rules' names
 * (`RULE_LABELS`: the Telegram card's, the Queue's and the CLI's "Approved by
 * YOLO"). Nothing here can import a Python constant, so `wire-contract.test.ts`
 * reads each back from that source and compares.
 *
 * NO IMPORTS. The Queue's rows draw `approvedBy` (`intents.ts`) and the sample
 * workspace draws the same rows, so this module reaches no door.
 */

/** A position: the workspace holds one, and a folder holds one or none (the workspace's). */
export const APPROVAL_POSITIONS = ["manual", "returning", "auto"] as const;

export type ApprovalPosition = (typeof APPROVAL_POSITIONS)[number];

/** The rule that approved a story. A person's approval has none. */
export const APPROVAL_RULES = ["auto", "item", "returning"] as const;

export type ApprovalRule = (typeof APPROVAL_RULES)[number];

/** How each rule is named where a person's name would stand. */
export const RULE_LABELS: Record<ApprovalRule, string> = {
  auto: "YOLO",
  item: "Auto-approve",
  returning: "Returning items",
};

/** A rule's name; one this tier does not know shows as itself, never as nothing (`vocabulary.rule_label`). */
export function ruleLabel(rule: string): string {
  return Object.hasOwn(RULE_LABELS, rule) ? RULE_LABELS[rule as ApprovalRule] : rule;
}

/** Each position's name in Settings. Two are the rules' own names, spelled once above. */
export const POSITION_LABELS: Record<ApprovalPosition, string> = {
  manual: "Ask every time",
  returning: RULE_LABELS.returning,
  auto: RULE_LABELS.auto,
};

/** What each position does, said under its name. */
export const POSITION_DESCRIPTIONS: Record<ApprovalPosition, string> = {
  manual: "Every story waits for a person.",
  returning:
    "An item that has posted through the API before goes out on its own; a new one waits.",
  auto: "Every story is approved automatically.",
};

function isApprovalPosition(value: unknown): value is ApprovalPosition {
  return typeof value === "string" && (APPROVAL_POSITIONS as readonly string[]).includes(value);
}

/**
 * A position read off the wire, or `null` when it is not one. Settings shows a
 * null as unavailable, never as Ask every time: a value this tier cannot read
 * is not the default.
 */
export function asApprovalPosition(value: unknown): ApprovalPosition | null {
  return isApprovalPosition(value) ? value : null;
}

/**
 * Workspace default, as a folder's select spells it. The select refuses an
 * empty value, and on the wire the choice is `null`.
 */
export const WORKSPACE_DEFAULT = "default";

/**
 * A folder's own position as the sources read serves it: a position, `null`
 * for Workspace default, or `undefined` when it is neither (an API that does
 * not send the field yet, or a position this tier does not know). Undefined
 * is shown as unavailable, never as Workspace default.
 */
export function folderPositionOf(raw: unknown): ApprovalPosition | null | undefined {
  if (raw === null) return null;
  return isApprovalPosition(raw) ? raw : undefined;
}

/** Why the Approvals card's controls are held: nothing reads a position while posting is by hand. */
const API_OFF_REASON = "Approvals act only while Instagram API posting is on.";

/** Why they are held for a member: positions are an admin's. */
const ADMIN_ONLY_REASON = "Only an admin can change approvals.";

/**
 * Each reason the card's controls are held, where it is the active one. API
 * posting counts as on only when the workspace says so: the sentence states
 * the rule, so it is true whatever was read.
 */
export function approvalsHeldBy(apiPublishing: boolean | null, editable: boolean): string[] {
  return [
    ...(apiPublishing === true ? [] : [API_OFF_REASON]),
    ...(editable ? [] : [ADMIN_ONLY_REASON]),
  ];
}

/** The question asked once before a choice that posts without a tap, and its answer's label. */
export type YoloQuestion = { title: string; body: string; confirm: string };

/**
 * What a choice that puts stories on YOLO asks, once, before it is sent: the
 * workspace's YOLO, one folder's own YOLO, or a folder
 * set to Workspace default while the workspace is on YOLO, which is YOLO by
 * another route.
 */
export function yoloQuestion(folderName: string | null, viaWorkspace = false): YoloQuestion {
  if (folderName === null) {
    return {
      title: "Turn on YOLO?",
      body: `Every story will post without a tap. Folders set to ${POSITION_LABELS.manual} still ask, and so do items with a link to add.`,
      confirm: "Turn on YOLO",
    };
  }
  if (viaWorkspace) {
    return {
      title: `Let ${folderName} follow the workspace's YOLO?`,
      body: `The workspace is on YOLO, so every story from ${folderName} will post without a tap. Items with a link to add still ask.`,
      confirm: "Follow the workspace",
    };
  }
  return {
    title: `Turn on YOLO for ${folderName}?`,
    body: `Every story from ${folderName} will post without a tap. Items with a link to add still ask.`,
    confirm: "Turn on YOLO",
  };
}

/** What the card says once the workspace's position is saved: a choice is otherwise silent. */
export function positionSavedNotice(position: ApprovalPosition): string {
  return `Saved. The workspace is on ${POSITION_LABELS[position]} from the next story.`;
}

/**
 * The library's Auto-approve toggle, explained on hover: what the flag does,
 * then what still asks. The toggle shows whatever the workspace's posting, as
 * Configure does on the card. A posting setting that could not be read adds no
 * sentence, since it may be on.
 */
export function autoApproveHint(
  item: { link_url: string | null },
  apiPublishing: boolean | null,
): string {
  return [
    `Each story of this item is approved automatically, even in a folder set to ${POSITION_LABELS.manual}.`,
    ...(apiPublishing === false ? ["It takes effect once Instagram API posting is on."] : []),
    ...(item.link_url ? ["This item has a link to add, and items with a link to add still ask."] : []),
  ].join(" ");
}
