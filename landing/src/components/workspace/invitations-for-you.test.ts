import { describe, expect, it, vi } from "vitest";
import type { ReactElement, ReactNode } from "react";
import { Button } from "@/components/ui/button";
import type { InvitationForYou } from "@/lib/invitations";
import { InvitationsForYouList } from "./invitations-for-you";

/**
 * The invitee's panel (115): someone who signed in without the link sees the
 * invitations waiting for them, where each link is, and one control each. No
 * DOM, as in `invite-member.test.ts`: the hook-free list reads as a tree.
 */

/** Depth-first walk of a React element tree, children flattened. */
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

/** Every string leaf in a tree, in document order. */
function strings(node: ReactNode): string[] {
  if (typeof node === "string") return [node];
  if (typeof node === "number") return [String(node)];
  if (node === null || node === undefined || typeof node !== "object") return [];
  if (Array.isArray(node)) return node.flatMap(strings);
  return strings((node as ReactElement<{ children?: ReactNode }>).props?.children);
}

const ACME: InvitationForYou = {
  id: "11111111-1111-4111-8111-111111111111",
  workspaceName: "Acme",
  role: "admin",
  expiresAt: "2026-10-18T12:00:00.000000+00:00",
  inviterName: "Dana",
};
const BETA: InvitationForYou = { ...ACME, id: "22222222-2222-4222-8222-222222222222", workspaceName: "Beta", role: "member", inviterName: null };

/** The panel over *invitations*, where re-sending can send unless told otherwise. */
function list(
  invitations: InvitationForYou[] | null = [ACME],
  over: Partial<Parameters<typeof InvitationsForYouList>[0]> = {},
  canResend = true,
) {
  return InvitationsForYouList({
    mine: invitations === null ? null : { invitations, canResend },
    said: {},
    busy: null,
    onResend: () => {},
    ...over,
  });
}

function buttons(tree: ReactNode) {
  return [...walk(tree)].filter((el) => el.type === Button);
}

describe("the invitations waiting for the signed-in person", () => {
  it("is nothing at all when none are waiting", () => {
    expect(list([])).toBeNull();
  });

  it("names the inviter, the workspace, where the link is and when it expires", () => {
    const text = strings(list()).join(" ");
    expect(text).toContain("Dana invited you to Acme as an admin.");
    expect(text).toContain("Open the invitation link you were sent to join.");
    expect(text).toContain("It expires Oct 18 (UTC).");
  });

  it("offers one control per invitation, and nothing that joins", () => {
    const tree = list([ACME, BETA]);
    const controls = buttons(tree);
    expect(controls).toHaveLength(2);
    for (const control of controls) {
      expect(strings(control).join("")).toBe("Email me the link again");
    }
    expect(strings(tree).join(" ")).not.toMatch(/accept|join now/i);
  });

  it("asks for the link of the invitation whose control was used", () => {
    const onResend = vi.fn();
    const [, second] = buttons(list([ACME, BETA], { onResend }));
    (second.props as { onClick: () => void }).onClick();
    expect(onResend).toHaveBeenCalledWith(BETA.id);
  });

  it("holds every control while one is sending, and says what the last one answered", () => {
    const tree = list([ACME, BETA], { busy: ACME.id, said: { [BETA.id]: "Sent." } });
    const controls = buttons(tree);
    expect(controls.map((c) => (c.props as { disabled: boolean }).disabled)).toEqual([true, true]);
    expect(strings(controls[0]).join("")).toBe("Sending…");
    const status = [...walk(tree)].filter(
      (el) => (el.props as { role?: string }).role === "status",
    );
    expect(status.map((el) => strings(el).join(""))).toEqual(["Sent."]);
  });

  it("says it couldn't check rather than showing none", () => {
    expect(strings(list(null)).join("")).toMatch(/couldn.t check for invitations/);
    expect(buttons(list(null))).toHaveLength(0);
  });

  it("where the link can't be sent again, says whom to ask and offers no button", () => {
    const tree = list([ACME, BETA], {}, false);
    expect(buttons(tree)).toHaveLength(0);
    const text = strings(tree).join(" ");
    expect(text).toContain("Don't have the link? Ask Dana to send it to you.");
    expect(text).toContain("Ask the person who invited you to send it to you.");
  });
});
