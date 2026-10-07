/**
 * The real Queue still sends what it sent before its rows moved into
 * `QueueView` (#1480).
 *
 * Two halves, both without a DOM (this suite runs `environment: "node"`):
 *
 *  1. THE WIRE. `sendQueueAction` against a stubbed `fetch`, for every action
 *     a row can carry, asserted as LITERALS — path, method, content type,
 *     body — never as values rebuilt through `requestFor` or `commandPath`,
 *     which would agree with a regression in either. Each body then goes
 *     through `parseCommand`, the route's own spec, so a body the route would
 *     refuse fails here rather than on a phone.
 *  2. THE WIRING. `QueueList` read as a returned element tree: it hands
 *     `QueueView` the matrix's levers, so the sample workspace's shorter list
 *     cannot leak into the real Queue, and an `onAction` that sends that same
 *     command and re-reads the list. `QueueList` holds state and `useState`
 *     outside a render throws, so React's `useState` and Next's `useRouter`
 *     are stubbed; everything else is the real module.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";

const hoisted = vi.hoisted(() => ({
  refresh: vi.fn(),
  /** One setter per `useState` call, in call order: pending, then notice. */
  setters: [] as ReturnType<typeof vi.fn>[],
  /** Values the next `useState` calls return instead of their initial state. */
  presets: [] as unknown[],
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
  };
});

import { parseCommand } from "@/lib/commands";
import { submitRescheduleItem } from "@/lib/command-client";
import { QUEUE_ACTIONS, type Intent, type IntentKeyedAction } from "@/lib/intents";
import { QueueView } from "./queue-view";
import { QueueList, sendQueueAction } from "./queue-list";

const WS = "11111111-1111-4111-8111-111111111111";
const INTENT_ID = "22222222-2222-4222-8222-222222222222";
const EPISODE = "2026-10-01T14:00:00.123456+00:00";

function intent(overrides: Partial<Intent> = {}): Intent {
  return {
    id: INTENT_ID,
    state: "awaiting_approval",
    ig_account_id: "33333333-3333-4333-8333-333333333333",
    media_item_id: "44444444-4444-4444-8444-444444444444",
    schedule_slot_at: "2026-10-01T14:00:00+00:00",
    approval_mode: "manual",
    published_via: null,
    publish_step: null,
    cancel_requested: false,
    ig_permalink: null,
    entered_state_at: EPISODE,
    created_at: "2026-10-01T13:00:00+00:00",
    origin: "cadence",
    scheduled_by_user_id: null,
    scheduled_by: null,
    tz: "UTC",
    miss_reason: null,
    link_url: null,
    file_name: "sample.jpg",
    media_kind: "image",
    thumbnail_url: null,
    caption: null,
    category: null,
    account_handle: "example.brand",
    account_display_name: "Example Co",
    ...overrides,
  };
}

type Captured = { url: string; init: RequestInit };

let captured: Captured[];

/** Reply with `body` at `status`, recording every call. */
function stubFetch(body: unknown = {}, status = 200) {
  captured = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      captured.push({ url, init });
      return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => body,
      } as unknown as Response;
    }),
  );
}

beforeEach(() => {
  vi.unstubAllGlobals();
  hoisted.refresh.mockClear();
  hoisted.setters.length = 0;
  hoisted.presets.length = 0;
});

/** What each intent-keyed action puts on the wire. Literal, on purpose. */
const WIRE: Record<IntentKeyedAction, { command: string; body: Record<string, unknown> }> = {
  approve: { command: "approve", body: { intent_id: INTENT_ID } },
  mark_posted: { command: "mark_posted", body: { intent_id: INTENT_ID } },
  skip: { command: "skip", body: { intent_id: INTENT_ID } },
  reject: { command: "reject", body: { intent_id: INTENT_ID } },
  cancel: { command: "cancel", body: { intent_id: INTENT_ID } },
  retry: {
    command: "resolve_review",
    body: { intent_id: INTENT_ID, resolution: "retry", verdict: "not_posted", episode: EPISODE },
  },
  resolve_posted: {
    command: "resolve_review",
    body: { intent_id: INTENT_ID, resolution: "posted", episode: EPISODE },
  },
  resolve_cancel: {
    command: "resolve_review",
    body: { intent_id: INTENT_ID, resolution: "cancel", episode: EPISODE },
  },
};

describe("sendQueueAction: the wire", () => {
  it("names every action a row can carry", () => {
    // A new action fails here until this table, or the Reschedule wire below
    // (keyed per submission, not on the intent), says what it sends.
    expect([...Object.keys(WIRE), "reschedule"].sort()).toEqual([...QUEUE_ACTIONS].sort());
  });

  it.each(Object.entries(WIRE))("%s", async (action, expected) => {
    stubFetch();
    await sendQueueAction(WS, intent(), action as IntentKeyedAction);

    expect(captured).toHaveLength(1);
    expect(captured[0].url).toBe(`/api/workspaces/${WS}/commands/${expected.command}`);
    expect(captured[0].init.method).toBe("POST");
    expect(captured[0].init.headers).toEqual({ "Content-Type": "application/json" });

    const sent = JSON.parse(String(captured[0].init.body));
    expect(sent).toEqual(expected.body);
    expect(parseCommand(expected.command, sent).ok).toBe(true);
  });

  it("answers with the route's refusal, unchanged", async () => {
    stubFetch({ error: "illegal_transition" }, 409);
    await expect(sendQueueAction(WS, intent(), "approve")).resolves.toEqual({
      ok: false,
      error: "illegal_transition",
      status: 409,
      body: { error: "illegal_transition" },
    });
  });
});

describe("Reschedule…: the wire", () => {
  it("sends the story and the time as typed, under a fresh submission", async () => {
    stubFetch({ outcome: "executed" });
    await submitRescheduleItem(WS, INTENT_ID, "2026-10-09T09:30");
    await submitRescheduleItem(WS, INTENT_ID, "2026-10-09T09:30");

    expect(captured.map((c) => c.url)).toEqual([
      `/api/workspaces/${WS}/commands/reschedule_item`,
      `/api/workspaces/${WS}/commands/reschedule_item`,
    ]);
    const [first, second] = captured.map((c) => JSON.parse(String(c.init.body)));
    expect(first).toEqual({
      intent_id: INTENT_ID,
      local_at: "2026-10-09T09:30",
      submission_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
    });
    // Moving a story twice is two acts: the same time twice is not a replay.
    expect(second.submission_id).not.toBe(first.submission_id);
    expect(parseCommand("reschedule_item", first).ok).toBe(true);
  });
});

describe("QueueList: the wiring", () => {
  const list = (apiPublishingEnabled = true) =>
    QueueList({
      workspaceId: WS,
      intents: [intent()],
      tz: "UTC",
      apiPublishingEnabled,
      truncatedAt: null,
    }) as ReactElement<Parameters<typeof QueueView>[0]>;

  it("draws its rows with QueueView", () => {
    expect(list().type).toBe(QueueView);
  });

  it("hands QueueView the matrix's levers", () => {
    expect(list(true).props.actionsOf(intent())).toEqual([
      "approve",
      "mark_posted",
      "skip",
      "reject",
    ]);
    expect(list(false).props.actionsOf(intent())).toEqual(["mark_posted", "skip", "reject"]);
    expect(
      list(true).props.actionsOf(
        intent({ state: "review_required", publish_step: "publish_called" }),
      ),
    ).toEqual(["resolve_posted", "retry", "resolve_cancel"]);
    expect(list(true).props.actionsOf(intent({ state: "approved" }))).toEqual([]);
    expect(
      list(true).props.actionsOf(intent({ state: "scheduled", origin: "planned" })),
    ).toEqual(["reschedule", "cancel"]);
    expect(list(true).props.actionsOf(intent({ state: "scheduled" }))).toEqual([]);
  });

  it("moves a planned story, then re-reads the list", async () => {
    stubFetch({ outcome: "executed" });
    const answer = await list().props.onReschedule!(intent(), "2026-10-09T09:30");

    expect(answer).toBeNull();
    expect(hoisted.refresh).toHaveBeenCalledTimes(1);
    expect(captured.map((c) => c.url)).toEqual([
      `/api/workspaces/${WS}/commands/reschedule_item`,
    ]);
  });

  it("answers a refused time with its sentence, for the dialog to show", async () => {
    stubFetch({ error: "invalid_args", facts: { at_rule: "past" } }, 400);
    const answer = await list().props.onReschedule!(intent(), "2020-01-01T09:00");

    expect(answer).toBe("That time has already passed on the account's clock. Pick a later one.");
    expect(hoisted.refresh).not.toHaveBeenCalled();
  });

  it("sends the tapped action for its workspace, then re-reads the list", async () => {
    stubFetch();
    list().props.onAction(intent(), "skip");

    await vi.waitFor(() => expect(hoisted.refresh).toHaveBeenCalledTimes(1));
    expect(captured.map((c) => c.url)).toEqual([`/api/workspaces/${WS}/commands/skip`]);
    expect(JSON.parse(String(captured[0].init.body))).toEqual({ intent_id: INTENT_ID });
  });

  it("says why under the row when the tap is refused, and re-reads after a refusal about the row", async () => {
    stubFetch({ error: "illegal_transition" }, 409);
    list().props.onAction(intent(), "approve");

    await vi.waitFor(() => expect(hoisted.refresh).toHaveBeenCalledTimes(1));
    const setNotice = hoisted.setters[1];
    expect(setNotice).toHaveBeenLastCalledWith({
      intentId: INTENT_ID,
      text: "This post already moved on — the list has been refreshed.",
    });
  });

  it("does not re-read after a refusal about the request", async () => {
    stubFetch({ error: "unauthenticated" }, 401);
    list().props.onAction(intent(), "approve");

    const setPending = hoisted.setters[0];
    // `pending` clears in the handler's `finally`: the last thing it does.
    await vi.waitFor(() => expect(setPending).toHaveBeenLastCalledWith(null));
    expect(captured).toHaveLength(1);
    expect(hoisted.refresh).not.toHaveBeenCalled();
  });

  it("puts a refusal under its own row as an alert, and nothing under the others", () => {
    hoisted.presets.push(null, { intentId: INTENT_ID, text: "Refused." });
    const props = list().props;

    expect(props.noteFor(intent())).toEqual({ text: "Refused.", tone: "alert" });
    expect(
      props.noteFor(intent({ id: "55555555-5555-4555-8555-555555555555" })),
    ).toBeNull();
  });
});
