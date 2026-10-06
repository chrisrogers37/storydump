import { readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { describe, expect, it } from "vitest";
import type { ReactElement, ReactNode } from "react";
import { SignOutButton } from "@/components/auth/sign-out-button";
import { ACCEPT_REFUSAL_COPY, AcceptRefusal } from "./accept-invitation";

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

describe("a refused accept for a link that can no longer be used (not_acceptable, #1603)", () => {
  // The API's one answer for an expired, used or withdrawn invitation, or one
  // whose sender or workspace no longer qualifies (`app.py`: 404 `not_acceptable`).
  const text = strings(AcceptRefusal({ reason: "not_acceptable", status: 404, token: "t" })).join("");

  it("says what may have happened and who can fix it, and does not blame the app", () => {
    expect(text).toMatch(/expired/);
    expect(text).toMatch(/already been used/);
    expect(text).toMatch(/withdrawn/);
    expect(text).toMatch(/ask the person who invited you for a new link/i);
    expect(text).not.toMatch(/on us/);
  });
});

describe("the page names only codes something sends", () => {
  // AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP (the `wire-contract` rule).
  const INVITATIONS = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    "../../../../src/services/target/invitations.py",
  );
  const reasons = (() => {
    const source = readFileSync(INVITATIONS, "utf8");
    const m = source.match(/^REASONS[^=]*=\s*\(([\s\S]*?)\)/m);
    if (!m) throw new Error(`no module-level REASONS tuple in ${INVITATIONS}`);
    return [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]);
  })();
  // The two that are not the API's: the route's own refusal of a malformed
  // token, and the browser's fetch throwing (`callBff`).
  const NOT_THE_APIS = ["invalid_invitation", "unreachable"];

  it("every code with a sentence is the API's, or the route's or the browser's own", () => {
    const strays = Object.keys(ACCEPT_REFUSAL_COPY).filter(
      (code) => !reasons.includes(code) && !NOT_THE_APIS.includes(code),
    );
    expect(strays).toEqual([]);
  });

  it("both codes the accept route answers have a sentence", () => {
    for (const code of ["not_acceptable", "identity_mismatch"]) {
      expect(reasons, code).toContain(code);
      expect(ACCEPT_REFUSAL_COPY[code], code).toBeTruthy();
    }
  });
});
