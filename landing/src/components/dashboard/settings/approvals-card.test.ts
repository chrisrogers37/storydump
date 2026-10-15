/**
 * Settings › General › Approvals (#1477): the workspace's position,
 * each folder's row, YOLO asking once, and the card held, with its reason,
 * while nothing would read a position.
 *
 * Without a DOM, as `queue-list.test.tsx` does it: the card and its rows are
 * called as functions and their returned trees read, with Next's `useRouter`
 * and React's state hooks stubbed. `useOptimistic` answers the server's value
 * unless a test says what a send in flight shows; `useTransition` runs the
 * action and records whether the re-read ran inside it. What a choice sends is
 * asserted on the wire, as literals, and run through the route's own spec.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactElement } from "react";

const hoisted = vi.hoisted(() => ({
  refresh: vi.fn(),
  /** One setter per `useState` call, in call order. */
  setters: [] as ReturnType<typeof vi.fn>[],
  /** Values the next `useState` calls return instead of their initial state. */
  presets: [] as unknown[],
  /** What the next `useOptimistic` shows instead of the server's value: a send in flight. */
  optimistic: [] as unknown[],
  /** One setter per `useOptimistic` call, in call order. */
  showers: [] as ReturnType<typeof vi.fn>[],
  /** Whether the next `useTransition` is pending: a send, or its re-read, in flight. */
  pending: [] as boolean[],
  /** Each transition started: settles once its action has, saying whether the re-read ran inside it. */
  transitions: [] as Promise<{ refreshedInside: boolean }>[],
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: hoisted.refresh }),
}));

vi.mock("react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react")>();
  return {
    ...actual,
    useState: (initial: unknown) => {
      const setter = vi.fn();
      hoisted.setters.push(setter);
      const value =
        hoisted.presets.length > 0
          ? hoisted.presets.shift()
          : typeof initial === "function"
            ? (initial as () => unknown)()
            : initial;
      return [value, setter];
    },
    useOptimistic: (value: unknown) => {
      const show = vi.fn();
      hoisted.showers.push(show);
      return [hoisted.optimistic.length > 0 ? hoisted.optimistic.shift() : value, show];
    },
    useTransition: () => [
      hoisted.pending.length > 0 ? hoisted.pending.shift() : false,
      (action: () => unknown) => {
        const before = hoisted.refresh.mock.calls.length;
        hoisted.transitions.push(
          Promise.resolve(action()).then(() => ({
            refreshedInside: hoisted.refresh.mock.calls.length > before,
          })),
        );
      },
    ],
  };
});

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Select, SelectItem, SelectTrigger } from "@/components/ui/select";
import type { ApprovalPosition } from "@/lib/approvals";
import { parseCommand } from "@/lib/commands";
import type { SourceRow } from "@/lib/dashboard-payloads";
import { ApprovalsCard, FolderApprovalRow, type Asking, type YoloAsk } from "./approvals-card";
import { Unavailable } from "./unavailable";

const WS = "11111111-1111-4111-8111-111111111111";
const TIPS = "22222222-2222-4222-8222-222222222222";
const EVENTS = "33333333-3333-4333-8333-333333333333";

function source(overrides: Partial<SourceRow>): SourceRow {
  return {
    id: TIPS,
    provider: "gdrive",
    state: "active",
    next_sync_at: null,
    last_sync_success_at: null,
    alerted_at: null,
    created_at: "2026-10-01T13:00:00+00:00",
    folder_ref: "folder-1",
    folder_name: "Tips",
    removed: false,
    approval_mode: null,
    ...overrides,
  };
}

const SOURCES: SourceRow[] = [
  source({ id: TIPS, folder_name: "Tips", approval_mode: "manual" }),
  source({ id: EVENTS, folder_name: "Events", folder_ref: "folder-2", approval_mode: null }),
  // Removed under Integrations: paused, and off the Drive card's list.
  source({ id: "44444444-4444-4444-8444-444444444444", folder_name: "Old", state: "paused", removed: true }),
];
const [TIPS_ROW, EVENTS_ROW] = SOURCES;

/** Every element in a returned tree, depth-first. */
function* walk(node: unknown): Generator<ReactElement> {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  if (!isValidElement(node)) return;
  yield node;
  yield* walk((node.props as { children?: unknown }).children);
}

/** The text a tree renders: its strings, in order. */
function text(node: unknown): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(text).join("");
  if (isValidElement(node)) return text((node.props as { children?: unknown }).children);
  return "";
}

type Captured = { url: string; init: RequestInit };
let captured: Captured[];
let fetchMock: ReturnType<typeof vi.fn>;

/** Reply with `body` at `status`, recording every call. */
function stubFetch(body: unknown = { outcome: "executed" }, status = 200) {
  captured = [];
  fetchMock = vi.fn(async (url: string, init: RequestInit) => {
    captured.push({ url, init });
    return { ok: status >= 200 && status < 300, status, json: async () => body } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
}

/** Whether the optimistic value was set before the command left: shown at once, not on the answer. */
const shownBeforeSent = (show: ReturnType<typeof vi.fn>) =>
  show.mock.invocationCallOrder[0] < fetchMock.mock.invocationCallOrder[0];

const sent = (i: number) => JSON.parse(String(captured[i].init.body));
const SUBMISSION = expect.stringMatching(/^[0-9a-f-]{36}$/);
const settled = () => Promise.all(hoisted.transitions);

beforeEach(() => {
  vi.unstubAllGlobals();
  stubFetch();
  hoisted.refresh.mockClear();
  hoisted.setters.length = 0;
  hoisted.presets.length = 0;
  hoisted.optimistic.length = 0;
  hoisted.showers.length = 0;
  hoisted.pending.length = 0;
  hoisted.transitions.length = 0;
});

// ── The card ──────────────────────────────────────────────────────────────

type CardProps = Parameters<typeof ApprovalsCard>[0];

/** The card's own state, by name; whatever is not named starts as a fresh card's. */
type CardState = { chosen?: ApprovalPosition | null; error?: string | null; savedTo?: ApprovalPosition | null; asking?: Asking | null };

/** Its setters, in the order the card calls `useState`. */
const CARD = { chosen: 0, seenPosition: 1, error: 2, savedTo: 3, asking: 4 } as const;
const cardSetter = (name: keyof typeof CARD) => hoisted.setters[CARD[name]];

function card(props: Partial<CardProps> = {}, state: CardState = {}) {
  const all: CardProps = {
    workspaceId: WS,
    position: "manual",
    sources: SOURCES,
    apiPublishing: true,
    editable: true,
    ...props,
  };
  hoisted.presets.push(
    state.chosen === undefined ? all.position : state.chosen,
    all.position,
    state.error ?? null,
    state.savedTo ?? null,
    state.asking ?? null,
  );
  return ApprovalsCard(all) as ReactElement;
}

type RadioProps = {
  value: string;
  checked: boolean;
  onChange: () => void;
  "aria-labelledby": string;
  "aria-describedby": string;
};
type RowProps = Parameters<typeof FolderApprovalRow>[0];
type ButtonProps = { onClick?: () => void; "aria-disabled"?: boolean; disabled?: boolean };

const radios = (tree: ReactElement) =>
  [...walk(tree)].filter(
    (el) => el.type === "input" && (el.props as { type?: string }).type === "radio",
  ) as ReactElement<RadioProps>[];
const radio = (tree: ReactElement, value: string) => radios(tree).find((r) => r.props.value === value)!;
const rows = (tree: ReactElement) =>
  [...walk(tree)].filter((el) => el.type === FolderApprovalRow) as ReactElement<RowProps>[];
const fieldset = (tree: ReactElement) =>
  [...walk(tree)].find((el) => el.type === "fieldset") as ReactElement<{
    disabled: boolean;
    "aria-describedby"?: string;
  }>;
/** The props of the button that holds exactly this label. */
const button = (tree: ReactElement, label: string) =>
  ([...walk(tree)].find((el) => el.type === Button && text(el) === label) as ReactElement<ButtonProps>)
    ?.props;
const dialog = (tree: ReactElement) =>
  [...walk(tree)].find((el) => el.type === Dialog) as ReactElement<{
    open: boolean;
    onOpenChange: (open: boolean) => void;
  }>;
const titleOf = (tree: ReactElement) => text([...walk(tree)].find((el) => el.type === DialogTitle));
const bodyOf = (tree: ReactElement) => text([...walk(tree)].find((el) => el.type === DialogDescription));

/** The question Save or a row put to the card, as the card's last `setAsking` holds it. */
const lastAsked = () => cardSetter("asking").mock.lastCall?.[0] as Asking;

describe("the Approvals card renders the three positions and the folders", () => {
  it("offers the workspace's three positions, with its own checked", () => {
    const tree = card({ position: "returning" });
    expect(radios(tree).map((r) => r.props.value)).toEqual(["manual", "returning", "auto"]);
    expect(radios(tree).map((r) => r.props.checked)).toEqual([false, true, false]);
    const labels = [...walk(tree)].filter((el) => el.type === "label").map(text);
    expect(labels).toEqual([
      "Ask every time" + "Every story waits for a person.",
      "Returning items" +
        "An item that has posted through the API before goes out on its own; a new one waits.",
      "YOLO" + "Every story is approved automatically.",
    ]);
  });

  it("names each position by its name alone, and describes it apart, for a screen reader", () => {
    const tree = card();
    const byId = (id: string) =>
      text([...walk(tree)].find((el) => (el.props as { id?: string }).id === id));
    expect(
      radios(tree).map((r) => [byId(r.props["aria-labelledby"]), byId(r.props["aria-describedby"])]),
    ).toEqual([
      ["Ask every time", "Every story waits for a person."],
      [
        "Returning items",
        "An item that has posted through the API before goes out on its own; a new one waits.",
      ],
      ["YOLO", "Every story is approved automatically."],
    ]);
  });

  it("gives each folder the Drive card lists a row of its own", () => {
    const listed = rows(card());
    expect(listed.map((r) => r.key)).toEqual([TIPS, EVENTS]);
    expect(listed.map((r) => r.props.source.id)).toEqual([TIPS, EVENTS]);
    for (const row of listed) {
      expect(row.props.workspaceOnYolo).toBe(false);
      expect(row.props.locked).toBe(false);
      expect(row.props.workspaceId).toBe(WS);
    }
  });

  it("keeps a paused auto folder visible and excludes a removed active folder", () => {
    const paused = source({ state: "paused", approval_mode: "auto" });
    const removed = source({ id: EVENTS, removed: true });
    const listed = rows(card({ sources: [paused, removed] }));
    expect(listed.map((r) => r.props.source.id)).toEqual([TIPS]);
    expect(listed[0].props.locked).toBe(false);
  });

  it("says an item's own Auto-approve wins over its folder, except for an item with a link to add", () => {
    // A story with a link to add is never approved automatically.
    expect(text(card())).toContain(
      "An item's own Auto-approve, set in the Media library, approves it whatever its folder says, unless it has a link to add.",
    );
  });

  it("says when no folder is connected", () => {
    expect(text(card({ sources: [] }))).toContain("No folder is connected yet.");
  });

  it("shows a position it could not read as unavailable, never as Ask every time", () => {
    const tree = card({ position: null });
    expect(radios(tree)).toEqual([]);
    expect([...walk(fieldset(tree))].some((el) => el.type === Unavailable)).toBe(true);
    expect(button(tree, "Save")).toBeUndefined();
    expect(rows(tree)).toHaveLength(2);
  });
});

describe("the card is held, with the reason, while API posting is off", () => {
  const REASON = "Approvals act only while Instagram API posting is on.";

  it("holds every control and says why, at the controls", () => {
    for (const apiPublishing of [false, null]) {
      const tree = card({ apiPublishing });
      expect(fieldset(tree).props.disabled, String(apiPublishing)).toBe(true);
      expect(rows(tree).map((r) => r.props.locked)).toEqual([true, true]);
      expect(rows(tree).map((r) => r.props.describedBy)).toEqual(["approvals-held", "approvals-held"]);
      const reason = [...walk(tree)].find((el) => (el.props as { id?: string }).id === "approvals-held");
      expect(text(reason)).toBe(REASON);
      expect(fieldset(tree).props["aria-describedby"]).toBe("approvals-held");
    }
  });

  it("is live, with no reason, once posting is on", () => {
    const tree = card({ apiPublishing: true });
    expect(fieldset(tree).props.disabled).toBe(false);
    expect(rows(tree).map((r) => r.props.locked)).toEqual([false, false]);
    expect(text(tree)).not.toContain(REASON);
    expect(fieldset(tree).props["aria-describedby"]).toBeUndefined();
  });

  it("is held for a member too, since positions are an admin's", () => {
    const tree = card({ editable: false });
    expect(fieldset(tree).props.disabled).toBe(true);
    expect(rows(tree).map((r) => r.props.locked)).toEqual([true, true]);
    expect(text(tree)).toContain("Only an admin can change approvals.");
    expect(text(tree)).not.toContain(REASON);
  });

  it("holds only Save while its save, or the re-read after it, is in flight, and disables nothing", async () => {
    hoisted.pending.push(true);
    const tree = card({}, { chosen: "returning" });
    expect(fieldset(tree).props.disabled).toBe(false);
    expect(rows(tree).map((r) => r.props.locked)).toEqual([false, false]);
    expect(button(tree, "Saving...")?.["aria-disabled"]).toBe(true);
    expect(button(tree, "Saving...")?.disabled).toBeUndefined();

    button(tree, "Saving...")!.onClick!();
    await Promise.resolve();
    expect(hoisted.transitions).toEqual([]);
    expect(captured).toEqual([]);
  });
});

describe("the workspace's position is chosen with the radios and sent with Save", () => {
  it("sends nothing as the radios move, so arrowing past a position saves nothing", () => {
    const tree = card();
    radio(tree, "returning").props.onChange();
    radio(tree, "auto").props.onChange();
    expect(cardSetter("chosen").mock.calls).toEqual([["returning"], ["auto"]]);
    expect(hoisted.transitions).toEqual([]);
    expect(cardSetter("asking")).not.toHaveBeenCalled();
  });

  it("holds Save until the choice differs from the workspace's", () => {
    expect(button(card(), "Save")?.["aria-disabled"]).toBe(true);
    hoisted.setters.length = 0;
    expect(button(card({}, { chosen: "returning" }), "Save")?.["aria-disabled"]).toBe(false);
  });

  it("sends the chosen position on Save, shows it at once, says it is saved, and re-reads inside the same transition", async () => {
    button(card({}, { chosen: "returning" }), "Save")!.onClick!();
    const [{ refreshedInside }] = await settled();

    expect(hoisted.showers[0].mock.calls).toEqual([["returning"]]);
    expect(shownBeforeSent(hoisted.showers[0])).toBe(true);
    expect(captured.map((c) => c.url)).toEqual([`/api/workspaces/${WS}/commands/settings_change`]);
    expect(sent(0)).toEqual({ settings: { approval_mode: "returning" }, submission_id: SUBMISSION });
    expect(parseCommand("settings_change", sent(0)).ok).toBe(true);
    expect(cardSetter("savedTo")).toHaveBeenLastCalledWith("returning");
    expect(refreshedInside).toBe(true);
  });

  it("sends nothing on Save when nothing changed", async () => {
    button(card(), "Save")!.onClick!();
    await Promise.resolve();
    expect(hoisted.transitions).toEqual([]);
  });

  it("says it is saved while the workspace is on the saved position, as shown or as read, and not after it moves", () => {
    const SAID = "Saved. The workspace is on Returning items from the next story.";
    const said = (position: ApprovalPosition, showing?: ApprovalPosition) => {
      hoisted.setters.length = 0;
      if (showing) hoisted.optimistic.push(showing);
      return text(card({ position }, { chosen: "returning", savedTo: "returning" })).includes(SAID);
    };
    expect(said("returning")).toBe(true);
    // The save still in flight: shown, not yet read.
    expect(said("manual", "returning")).toBe(true);
    // Another admin's change, read later: the line would be untrue.
    expect(said("manual")).toBe(false);
  });

  it("tells the rows the workspace is on YOLO while YOLO is being saved, so Workspace default asks", () => {
    hoisted.optimistic.push("auto");
    expect(rows(card({ position: "manual" })).map((r) => r.props.workspaceOnYolo)).toEqual([true, true]);
  });

  it("still tells the rows the workspace is on YOLO while a save away from it is in flight", () => {
    // The save may be refused, which would leave a folder on Workspace default on YOLO, unasked.
    hoisted.optimistic.push("manual");
    expect(rows(card({ position: "auto" })).map((r) => r.props.workspaceOnYolo)).toEqual([true, true]);
  });

  it("tells the rows the workspace is not on YOLO when it is neither read nor being saved so", () => {
    hoisted.optimistic.push("returning");
    expect(rows(card({ position: "manual" })).map((r) => r.props.workspaceOnYolo)).toEqual([false, false]);
  });

  it("takes the workspace's new position, once a re-read moves it, in place", () => {
    hoisted.presets.push("returning", "returning", null, null, null);
    ApprovalsCard({ workspaceId: WS, position: "manual", sources: SOURCES, apiPublishing: true, editable: true });
    expect(cardSetter("chosen")).toHaveBeenCalledWith("manual");
    expect(cardSetter("seenPosition")).toHaveBeenCalledWith("manual");
  });

  it("says why when Save is refused, in the settings' words, keeping the choice unsaved, with no undo", async () => {
    stubFetch({ detail: "forbidden" }, 403);
    button(card({}, { chosen: "returning" }), "Save")!.onClick!();
    await settled();

    expect(cardSetter("error")).toHaveBeenLastCalledWith(
      "You do not have permission to change this. An admin or the workspace owner can.",
    );
    expect(cardSetter("chosen")).not.toHaveBeenCalled();
    expect(cardSetter("savedTo").mock.calls).toEqual([[null]]);
    // No undo: the shown position falls back to the server's when the transition ends.
    expect(hoisted.showers[0].mock.calls).toEqual([["returning"]]);
    expect(hoisted.refresh).not.toHaveBeenCalled();
  });
});

describe("YOLO asks once", () => {
  const WORKSPACE_SENTENCE =
    "Every story will post without a tap. Folders set to Ask every time still ask, and so do items with a link to add.";
  const confirmSpy = vi.fn();
  const keepSpy = vi.fn();
  const ASKING: Asking = {
    question: {
      title: "Turn on YOLO?",
      body: WORKSPACE_SENTENCE,
      confirm: "Turn on YOLO",
    },
    opener: "approval-position-save",
    confirm: confirmSpy,
    keep: keepSpy,
    open: true,
  };

  afterEach(() => {
    confirmSpy.mockClear();
    keepSpy.mockClear();
  });

  it("asks instead of sending when YOLO is saved", () => {
    button(card({}, { chosen: "auto" }), "Save")!.onClick!();
    const asked = lastAsked();
    expect(asked.open).toBe(true);
    expect(asked.question).toEqual({ title: "Turn on YOLO?", body: WORKSPACE_SENTENCE, confirm: "Turn on YOLO" });
    expect(asked.opener).toBe("approval-position-save");
    expect(hoisted.transitions).toEqual([]);
  });

  it("sends the workspace's YOLO once it is confirmed", async () => {
    button(card({}, { chosen: "auto" }), "Save")!.onClick!();
    lastAsked().confirm();
    await settled();
    expect(captured.map((c) => c.url)).toEqual([`/api/workspaces/${WS}/commands/settings_change`]);
    expect(sent(0)).toEqual({ settings: { approval_mode: "auto" }, submission_id: SUBMISSION });
  });

  it("puts the radios back on the workspace's position when the workspace's YOLO is kept", () => {
    button(card({ position: "manual" }, { chosen: "auto" }), "Save")!.onClick!();
    lastAsked().keep!();
    expect(cardSetter("chosen")).toHaveBeenLastCalledWith("manual");
    expect(hoisted.transitions).toEqual([]);
  });

  it("shows the question asked, and answers yes once", () => {
    const tree = card({}, { asking: ASKING });
    expect(dialog(tree).props.open).toBe(true);
    expect(titleOf(tree)).toBe("Turn on YOLO?");
    expect(bodyOf(tree)).toBe(WORKSPACE_SENTENCE);

    button(tree, "Turn on YOLO")!.onClick!();
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(cardSetter("asking")).toHaveBeenLastCalledWith({ ...ASKING, open: false });
  });

  it("sends once, however often its answer is clicked", () => {
    // Answered, and fading: a second click does nothing.
    button(card({}, { asking: { ...ASKING, open: false } }), "Turn on YOLO")!.onClick!();
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("sends nothing when kept, and puts back what the choice changed", () => {
    const tree = card({}, { asking: ASKING });
    expect(button(tree, "Keep it")?.onClick).toBeUndefined();
    dialog(tree).props.onOpenChange(false);
    expect(keepSpy).toHaveBeenCalledTimes(1);
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(cardSetter("asking")).toHaveBeenLastCalledWith({ ...ASKING, open: false });
  });

  it("asks a row's question, and gives focus back to the control that asked", () => {
    const row = rows(card())[1];
    const ask: YoloAsk = { ...ASKING, opener: `approval-folder-${EVENTS}` };
    row.props.onAsk(ask);
    expect(cardSetter("asking")).toHaveBeenLastCalledWith({ ...ask, open: true });

    hoisted.setters.length = 0;
    const tree = card({}, { asking: { ...ask, open: false } });
    const content = [...walk(tree)].find((el) => el.type === DialogContent) as ReactElement<{
      onCloseAutoFocus: (event: { preventDefault: () => void }) => void;
    }>;
    const focus = vi.fn();
    const getElementById = vi.fn(() => ({ focus }));
    vi.stubGlobal("document", { getElementById });
    const event = { preventDefault: vi.fn() };
    content.props.onCloseAutoFocus(event);
    expect(getElementById).toHaveBeenCalledWith(`approval-folder-${EVENTS}`);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(focus).toHaveBeenCalled();
  });
});

// ── A folder's row ────────────────────────────────────────────────────────

type SelectProps = {
  value: string;
  disabled: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onValueChange: (value: string) => void;
};

const onAsk = vi.fn();

function row(props: Partial<RowProps> = {}) {
  return FolderApprovalRow({
    workspaceId: WS,
    source: EVENTS_ROW,
    workspaceOnYolo: false,
    locked: false,
    onAsk,
    ...props,
  }) as ReactElement;
}

const select = (tree: ReactElement) =>
  [...walk(tree)].find((el) => el.type === Select) as ReactElement<SelectProps> | undefined;
const trigger = (tree: ReactElement) =>
  [...walk(tree)].find((el) => el.type === SelectTrigger) as ReactElement<{
    id: string;
    "aria-disabled"?: boolean;
    "aria-describedby"?: string;
    onKeyDown: (event: { key: string; preventDefault: () => void }) => void;
  }>;
/** The question the row last put to the card. */
const askedOf = () => onAsk.mock.lastCall?.[0] as YoloAsk;

describe("a folder's row", () => {
  afterEach(() => onAsk.mockClear());

  it("holds its folder's own position, or Workspace default, in a select named by the folder", () => {
    const tips = row({ source: TIPS_ROW });
    expect(select(tips)!.props.value).toBe("manual");
    expect(select(row())!.props.value).toBe("default");
    const options = [...walk(select(tips))].filter((el) => el.type === SelectItem);
    expect(options.map((o) => (o.props as { value: string }).value)).toEqual([
      "default",
      "manual",
      "returning",
      "auto",
    ]);
    expect(options.map(text)).toEqual(["Workspace default", "Ask every time", "Returning items", "YOLO"]);
    expect(trigger(tips).props.id).toBe(`approval-folder-${TIPS}`);
    const label = [...walk(tips)].find(
      (el) => (el.props as { htmlFor?: string }).htmlFor === `approval-folder-${TIPS}`,
    );
    expect(text(label)).toBe("Tips");
  });

  it("shows a position it could not read as unavailable, never as Workspace default", () => {
    // Missing (an API that does not send it yet) or a position this tier does not know.
    for (const raw of [undefined, "sometimes"]) {
      const tree = row({ source: source({ id: EVENTS, folder_name: "Events", approval_mode: raw }) });
      expect(select(tree), String(raw)).toBeUndefined();
      expect([...walk(tree)].some((el) => el.type === Unavailable)).toBe(true);
      expect(text(tree)).toContain("Events");
    }
  });

  it("sends Workspace default as null, shows it at once, then re-reads inside the same transition", async () => {
    // Tips holds its own Ask every time; the workspace is not on YOLO.
    select(row({ source: TIPS_ROW }))!.props.onValueChange("default");
    const [{ refreshedInside }] = await settled();

    expect(onAsk).not.toHaveBeenCalled();
    expect(hoisted.showers[0].mock.calls).toEqual([[null]]);
    expect(shownBeforeSent(hoisted.showers[0])).toBe(true);
    expect(captured.map((c) => c.url)).toEqual([`/api/workspaces/${WS}/commands/set_source_approval`]);
    const body = sent(0);
    expect(body).toEqual({ source_id: TIPS, approval_mode: null, submission_id: SUBMISSION });
    expect("approval_mode" in body).toBe(true);
    expect(parseCommand("set_source_approval", body).ok).toBe(true);
    expect(refreshedInside).toBe(true);
  });

  it("shows Paused and lets an auto folder change its position after disconnect", async () => {
    const tree = row({ source: source({ state: "paused", approval_mode: "auto" }) });
    expect(text(tree)).toContain("Paused");
    expect(select(tree)!.props.value).toBe("auto");
    expect(select(tree)!.props.disabled).toBe(false);
    select(tree)!.props.onValueChange("manual");
    await settled();
    expect(sent(0)).toEqual({ source_id: TIPS, approval_mode: "manual", submission_id: SUBMISSION });
    expect(onAsk).not.toHaveBeenCalled();
  });

  it("sends a folder's own position", async () => {
    select(row())!.props.onValueChange("returning");
    await settled();
    expect(sent(0)).toEqual({ source_id: EVENTS, approval_mode: "returning", submission_id: SUBMISSION });
  });

  it("asks for a folder's own YOLO, naming it, then sends that folder's position once it is confirmed", async () => {
    select(row())!.props.onValueChange("auto");
    expect(hoisted.transitions).toEqual([]);
    expect(askedOf().question).toEqual({
      title: "Turn on YOLO for Events?",
      body: "Every story from Events will post without a tap. Items with a link to add still ask.",
      confirm: "Turn on YOLO",
    });
    expect(askedOf().opener).toBe(`approval-folder-${EVENTS}`);

    askedOf().confirm();
    await settled();
    expect(sent(0)).toEqual({ source_id: EVENTS, approval_mode: "auto", submission_id: SUBMISSION });
  });

  it("asks before a folder follows a workspace that is on YOLO, which is YOLO by another route", async () => {
    select(row({ source: TIPS_ROW, workspaceOnYolo: true }))!.props.onValueChange("default");
    expect(hoisted.transitions).toEqual([]);
    expect(askedOf().question).toEqual({
      title: "Let Tips follow the workspace's YOLO?",
      body: "The workspace is on YOLO, so every story from Tips will post without a tap. Items with a link to add still ask.",
      confirm: "Follow the workspace",
    });

    askedOf().confirm();
    await settled();
    expect(sent(0)).toEqual({ source_id: TIPS, approval_mode: null, submission_id: SUBMISSION });
  });

  it("says why under the row when refused, in its own words, with no undo", async () => {
    stubFetch({ error: "not_found" }, 404);
    select(row({ source: TIPS_ROW }))!.props.onValueChange("returning");
    await settled();

    const setRefusal = hoisted.setters[0];
    expect(setRefusal).toHaveBeenLastCalledWith("That folder is no longer here. Reload the page.");
    // No undo: the shown position falls back to the server's when the transition ends.
    expect(hoisted.showers[0].mock.calls).toEqual([["returning"]]);
    expect(hoisted.refresh).not.toHaveBeenCalled();
  });

  it("shows a refusal as an alert under the row", () => {
    hoisted.presets.push("Refused.");
    const alert = [...walk(row())].find(
      (el) => el.type === "p" && (el.props as { role?: string }).role === "alert",
    );
    expect(text(alert)).toBe("Refused.");
  });

  it("holds itself while its command, or the re-read after it, is in flight, without disabling it", async () => {
    hoisted.pending.push(true);
    const tree = row();
    expect(trigger(tree).props["aria-disabled"]).toBe(true);
    expect(select(tree)!.props.disabled).toBe(false);
    select(tree)!.props.onValueChange("returning");
    await Promise.resolve();
    expect(hoisted.transitions).toEqual([]);
    expect(captured).toEqual([]);
  });

  it("does not open while held, so it never takes a pick it would drop", () => {
    hoisted.pending.push(true);
    select(row())!.props.onOpenChange(true);
    const setOpen = hoisted.setters[1];
    expect(setOpen).not.toHaveBeenCalled();
  });

  it("takes no typed pick while held, as a closed trigger's typeahead would", () => {
    hoisted.pending.push(true);
    const held = { key: "r", preventDefault: vi.fn() };
    trigger(row()).props.onKeyDown(held);
    expect(held.preventDefault).toHaveBeenCalledTimes(1);
    // Navigation keys still reach the trigger, and an unheld row types freely.
    const tab = { key: "Tab", preventDefault: vi.fn() };
    trigger(row()).props.onKeyDown(tab);
    expect(tab.preventDefault).not.toHaveBeenCalled();
  });

  it("types freely when it is not held", () => {
    const free = { key: "r", preventDefault: vi.fn() };
    trigger(row()).props.onKeyDown(free);
    expect(free.preventDefault).not.toHaveBeenCalled();
  });

  it("opens, and closes, when it is not held", () => {
    const tree = row();
    expect(select(tree)!.props.open).toBe(false);
    select(tree)!.props.onOpenChange(true);
    select(tree)!.props.onOpenChange(false);
    expect(hoisted.setters[1].mock.calls).toEqual([[true], [false]]);
  });

  it("sends and shows a pick on one folder while another folder's save is in flight", async () => {
    // Tips' save is in flight; Events' row is its own.
    hoisted.pending.push(true, false);
    const tips = row({ source: TIPS_ROW });
    const events = row();

    select(events)!.props.onValueChange("returning");
    select(tips)!.props.onValueChange("returning");
    await settled();

    expect(hoisted.transitions).toHaveLength(1);
    expect(hoisted.showers[1].mock.calls).toEqual([["returning"]]);
    expect(hoisted.showers[0]).not.toHaveBeenCalled();
    expect(captured.map((_, i) => sent(i).source_id)).toEqual([EVENTS]);
  });

  it("is held, with the card's reason, while the card is", async () => {
    const tree = row({ locked: true, describedBy: "approvals-held" });
    expect(select(tree)!.props.disabled).toBe(true);
    expect(trigger(tree).props["aria-describedby"]).toBe("approvals-held");
    select(tree)!.props.onValueChange("returning");
    await Promise.resolve();
    expect(hoisted.transitions).toEqual([]);
  });
});
