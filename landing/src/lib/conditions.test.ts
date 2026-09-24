/**
 * The condition surface's derivation: which facts the database already holds
 * become a line on the overview, in which words, pointing where.
 *
 * The words are asserted against the tabs' own vocabularies
 * (`destinationStateBadge`, `destinationConnectionCaption`) rather than
 * retyped, because the property is that the panel and the tab say the same
 * thing about the same row — a person told "Reconnect needed" here must find
 * "Reconnect needed" when they follow the link.
 */

import { describe, expect, it } from "vitest";
import {
  ACCOUNTS_HREF,
  ALL_CLEAR_DETAIL,
  INTEGRATIONS_HREF,
  QUEUE_HREF,
  deriveConditions,
  type ConditionInputs,
} from "./conditions";
import {
  destinationConnectionCaption,
  destinationStateBadge,
} from "./destination";

type AccountInput = ConditionInputs["accounts"][number];
type SourceInput = ConditionInputs["sources"][number];

function account(over: Partial<AccountInput> = {}): AccountInput {
  return {
    id: "acc-1",
    handle: "storyco",
    display_name: null,
    state: "active",
    credential_status: "active",
    ...over,
  };
}

function source(over: Partial<SourceInput> = {}): SourceInput {
  return { id: "src-1", state: "active", folder_name: "Summer", ...over };
}

const NOTHING: ConditionInputs = { accounts: [], sources: [], intentsByState: {} };

describe("deriveConditions — a healthy workspace", () => {
  it("has no conditions when every destination, folder and post is in order", () => {
    expect(
      deriveConditions({
        accounts: [account()],
        sources: [source()],
        intentsByState: { scheduled: 4, awaiting_approval: 2, posted: 10 },
      }),
    ).toEqual([]);
  });

  it("has no conditions when there is nothing yet — an empty workspace is not a fault", () => {
    expect(deriveConditions(NOTHING)).toEqual([]);
  });
});

describe("deriveConditions — destinations (Accounts)", () => {
  it("names a destination that needs reconnecting and sends the person to Accounts", () => {
    expect(
      deriveConditions({ ...NOTHING, accounts: [account({ state: "reauth_required" })] }),
    ).toEqual([
      {
        key: "account:acc-1",
        text: "storyco — Reconnect needed",
        href: ACCOUNTS_HREF,
        action: "Open Accounts",
      },
    ]);
  });

  it.each(["reauth_required", "moved", "a-state-this-build-does-not-know"])(
    "a destination in state %s is a condition, in its badge's own words",
    (state) => {
      const [line] = deriveConditions({ ...NOTHING, accounts: [account({ state })] });
      expect(line.text).toBe(`storyco — ${destinationStateBadge(state).label}`);
      expect(line.href).toBe(ACCOUNTS_HREF);
    },
  );

  it.each(["expired", "revoked"] as const)(
    "an active destination whose Instagram access is %s is a condition, as the Accounts tab says",
    (credential_status) => {
      const [line] = deriveConditions({
        ...NOTHING,
        accounts: [account({ credential_status })],
      });
      expect(line.text).toBe(`storyco — ${destinationConnectionCaption(credential_status)}`);
      expect(line.href).toBe(ACCOUNTS_HREF);
    },
  );

  it("a destination posted to by hand (never connected to Instagram) is not a condition", () => {
    expect(
      deriveConditions({ ...NOTHING, accounts: [account({ credential_status: "none" })] }),
    ).toEqual([]);
  });

  it("a destination wrong in both ways is ONE line, and its state is the reason given", () => {
    const lines = deriveConditions({
      ...NOTHING,
      accounts: [account({ state: "reauth_required", credential_status: "revoked" })],
    });
    expect(lines).toHaveLength(1);
    expect(lines[0].text).toBe("storyco — Reconnect needed");
  });

  it("names the destination the way its Accounts row is titled", () => {
    const [line] = deriveConditions({
      ...NOTHING,
      accounts: [account({ display_name: "Story Co", state: "reauth_required" })],
    });
    expect(line.text).toBe("Story Co — Reconnect needed");
  });
});

describe("deriveConditions — folders (Integrations)", () => {
  it("names a folder whose sync failed and sends the person to Integrations", () => {
    expect(deriveConditions({ ...NOTHING, sources: [source({ state: "error" })] })).toEqual([
      {
        key: "source:src-1",
        text: "Summer — Stopped syncing",
        href: INTEGRATIONS_HREF,
        action: "Open Integrations",
      },
    ]);
  });

  it("a folder the picker never named reads as the Drive card reads it", () => {
    const [line] = deriveConditions({
      ...NOTHING,
      sources: [source({ state: "error", folder_name: null })],
    });
    expect(line.text).toBe("Drive folder — Stopped syncing");
  });

  it("a paused folder — removed, or its grant disconnected — is a decision, not a condition", () => {
    expect(deriveConditions({ ...NOTHING, sources: [source({ state: "paused" })] })).toEqual([]);
  });
});

describe("deriveConditions — posts (Queue)", () => {
  it.each([
    [1, "1 post needs a decision"],
    [3, "3 posts need a decision"],
  ])("%i review_required post(s) read as %j and send the person to the Queue", (n, text) => {
    expect(
      deriveConditions({ ...NOTHING, intentsByState: { review_required: n } }),
    ).toEqual([{ key: "review_required", text, href: QUEUE_HREF, action: "Open Queue" }]);
  });

  it("only review_required is a decision the workspace owes — the rest is the Queue's ordinary work", () => {
    expect(
      deriveConditions({
        ...NOTHING,
        intentsByState: {
          review_required: 0,
          awaiting_approval: 5,
          publishing_ambiguous: 1,
          failed: 2,
        },
      }),
    ).toEqual([]);
  });
});

describe("deriveConditions — the panel as a whole", () => {
  it("lists destinations, then folders, then posts, one line each with a distinct key", () => {
    const lines = deriveConditions({
      accounts: [
        account({ id: "a1", state: "reauth_required" }),
        account({ id: "a2" }),
        account({ id: "a3", handle: "other", credential_status: "expired" }),
      ],
      sources: [source({ id: "s1", state: "error" }), source({ id: "s2" })],
      intentsByState: { review_required: 2 },
    });
    expect(lines.map((l) => l.key)).toEqual([
      "account:a1",
      "account:a3",
      "source:s1",
      "review_required",
    ]);
  });

  it("the all-clear sentence names every kind of condition checked, so it claims no more than was looked at", () => {
    // One clause per kind `deriveConditions` reads. A new kind is a new clause.
    expect(ALL_CLEAR_DETAIL).toMatch(/account/);
    expect(ALL_CLEAR_DETAIL).toMatch(/folder/);
    expect(ALL_CLEAR_DETAIL).toMatch(/post/);
  });
});
