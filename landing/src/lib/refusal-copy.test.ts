import { describe, expect, it } from "vitest";

import { settingsRefusalCopy } from "./command-client";
import { addDestinationRefusalCopy } from "./destination";
import { refusalCopy } from "./intents";
import {
  createWorkspaceRefusalCopy,
  notAuthenticatedCopy,
  type RefusalOutcome,
} from "./refusal-copy";
import { addSourceRefusalCopy, connectRefusalCopy } from "./source-connect";

/**
 * #1140 — the six sites that collapsed roughly seven causes into one confident
 * sentence, pinned together.
 *
 * **Why this file exists rather than six more rows in six existing files.** The
 * test that was supposed to cover this already existed: `destination.test.ts`
 * asserted `["unauthenticated", "session expired"]` under a describe block
 * whose stated contract is *"names what happened"*. It could not fail for the
 * defect it was named after, because the assertion only checked the string
 * contained the fragment the same table had chosen — it would have passed for
 * any wrong-but-consistent wording, and the wrong wording was in the table.
 * A per-site fragment test reproduces exactly that: six tables, six chances to
 * write down the same guess and bless it in review.
 *
 * So the pin is equality against ONE shared function. There is nowhere to
 * record a second opinion: a site that stops calling it fails here, and the
 * only way to change the sentence is to change it for all six at once, in a
 * place where the reasoning is written down.
 *
 * **Bound, stated because a green run here should not be read as more than it
 * is:** no type or test can check that a sentence is TRUE of seven causes. What
 * is mechanised is that the six sites cannot disagree, that the outcome clause
 * is one of a closed set, and that the sentence does not name a cause. Whether
 * "you are not signed in, or the app could not prove it" really covers every
 * way a 401 arises is a judgement, made in `refusal-copy.ts` and reviewable
 * there.
 */

/** Every reason that reaches a not-authenticated branch, per site. */
const SITES: ReadonlyArray<{
  name: string;
  outcome: RefusalOutcome;
  copy: (reason: unknown) => string;
  reasons: readonly string[];
}> = [
  {
    name: "addDestinationRefusalCopy",
    outcome: "Nothing was added.",
    copy: addDestinationRefusalCopy,
    reasons: ["unauthenticated", "http_401"],
  },
  {
    name: "addSourceRefusalCopy",
    outcome: "Nothing was added.",
    copy: addSourceRefusalCopy,
    reasons: ["unauthenticated", "http_401"],
  },
  {
    name: "connectRefusalCopy",
    outcome: "Nothing changed.",
    copy: connectRefusalCopy,
    reasons: ["unauthenticated", "http_401"],
  },
  {
    name: "settingsRefusalCopy",
    outcome: "Nothing changed.",
    copy: settingsRefusalCopy,
    reasons: ["unauthenticated"],
  },
  {
    name: "refusalCopy (queue)",
    outcome: "Nothing changed.",
    copy: refusalCopy,
    reasons: ["unauthenticated"],
  },
  {
    // Keyed on STATUS, not reason — the shape the original defect was written
    // in. Included by driving the status it actually branches on.
    name: "createWorkspaceRefusalCopy",
    outcome: "Nothing was created.",
    copy: (reason: unknown) => createWorkspaceRefusalCopy(reason, 401),
    reasons: ["unauthenticated", "anything_else_with_a_401"],
  },
];

describe("the not-authenticated sentence is single-sourced", () => {
  it("covers every site #1140 named", () => {
    // The count is asserted so that deleting a site from the table — the easy
    // way to make this file green — is itself a failure.
    expect(SITES).toHaveLength(6);
  });

  it.each(SITES)(
    "$name returns the shared sentence for every reason that reaches it",
    (site) => {
      for (const reason of site.reasons) {
        expect(site.copy(reason)).toBe(notAuthenticatedCopy(site.outcome));
      }
    },
  );
});

describe("the sentence itself", () => {
  const sentence = notAuthenticatedCopy("Nothing changed.");

  it("names no cause, because the code distinguished none", () => {
    // The seven a 401 admits. The point of the fix is that the sentence is
    // true whichever fired, so naming any of them is the defect returning.
    for (const cause of [
      "expired",
      "revoked",
      "malformed",
      "clock",
      "audience",
    ]) {
      expect(sentence.toLowerCase()).not.toContain(cause);
    }
  });

  it("states what was observed", () => {
    expect(sentence).toContain("You are not signed in, or the app could not prove it.");
  });

  it("states what did not happen, so a retry is an informed choice", () => {
    expect(sentence).toContain("Nothing changed.");
  });

  it("hedges the remedy and offers an escalation", () => {
    // "Sign in again." flat is the original defect: a remedy is a claim, and
    // for the never-sent cause signing in again cannot work.
    expect(sentence).toContain("may help");
    expect(sentence).toContain("worth reporting");
    expect(sentence).not.toContain("Sign in again.");
  });
});

describe("createWorkspaceRefusalCopy keeps the branches that were already right", () => {
  it("still asks for a name", () => {
    expect(createWorkspaceRefusalCopy("invalid_name", 400)).toContain(
      "Give the workspace a name.",
    );
  });

  it("still says a 503 is not the user's doing", () => {
    expect(createWorkspaceRefusalCopy(undefined, 503)).toContain("Nothing you did");
  });

  it("still takes the blame for a cause it cannot name", () => {
    // Deliberately untouched by #1140: this is the one message that was already
    // honest — it names no cause and does not tell anyone to do something that
    // may not work.
    expect(createWorkspaceRefusalCopy("something_new", 500)).toBe(
      "That did not work. This one is on us.",
    );
  });
});
