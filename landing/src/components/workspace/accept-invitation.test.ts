import { describe, expect, it } from "vitest";
import type { ReactElement, ReactNode } from "react";
import { SignOutButton } from "@/components/auth/sign-out-button";
import { AcceptRefusal } from "./accept-invitation";

/**
 * What the accept control says when the accept call is refused (#1568), read as
 * an element tree: no DOM, as in `api-tokens-tab.test.ts`.
 */

function* walk(node: ReactNode): Generator<ReactElement> {
  if (node === null || node === undefined || typeof node !== "object") return;
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  const el = node as ReactElement<{ children?: ReactNode }>;
  yield el;
  yield* walk(el.props?.children);
}

function strings(node: ReactNode): string[] {
  if (typeof node === "string") return [node];
  if (typeof node === "number") return [String(node)];
  if (node === null || node === undefined || typeof node !== "object") return [];
  if (Array.isArray(node)) return node.flatMap(strings);
  return strings((node as ReactElement<{ children?: ReactNode }>).props?.children);
}

const signOuts = (tree: ReactNode) => [...walk(tree)].filter((el) => el.type === SignOutButton);

describe("a refused accept, signed in as the wrong Google account (identity_mismatch)", () => {
  // The API's code for an acceptor whose Google address is not the invited one
  // (`app.py`: 403 `identity_mismatch`).
  const tree = AcceptRefusal({ reason: "identity_mismatch", status: 403, token: "tok/en" });

  it("says it is the account, not the app, and what to do", () => {
    const text = strings(tree).join("");
    expect(text).toMatch(/different Google account/);
    expect(text).toMatch(/sign in with the one it was sent to/i);
    expect(text).not.toMatch(/on us/);
  });

  it("offers the sign-out right there, landing back on this invitation", () => {
    const buttons = signOuts(tree);
    expect(buttons).toHaveLength(1);
    // Back to the invitation, not /login: only /join/[token]/start sets the
    // cookie that carries the invitation through the next sign-in.
    expect((buttons[0].props as { redirectTo: string }).redirectTo).toBe("/join/tok%2Fen");
  });
});

describe("any other refusal", () => {
  it("offers no sign-out, since signing in again would not help", () => {
    for (const reason of ["not_acceptable", "unreachable", "http_500"]) {
      expect(signOuts(AcceptRefusal({ reason, status: 404, token: "t" })), reason).toEqual([]);
    }
  });
});
