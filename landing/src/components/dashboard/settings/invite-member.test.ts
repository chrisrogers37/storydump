import { describe, expect, it, vi } from "vitest";
import type { ReactElement, ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/setup/copy-button";
import type { PendingInvitation } from "@/lib/invitations";
import { InviteLinkBlock, PendingInvitations, inviteLinkSlot } from "./invite-member";

/**
 * The render contract of inviting a person (#1563, #1564): THE JOIN LINK IS ON
 * SCREEN ONLY WHILE ONE IS IN STATE, and then in exactly one place, the copy
 * control. Plus the pending list an admin sees beside it.
 *
 * NO DOM, as in `api-tokens-tab.test.ts`: the form calls hooks, so the blocks
 * that show the link and the list are hook-free and read as element trees.
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

const LINK = "https://storydump.app/join/" + "q".repeat(43);
const MINTED = { link: LINK, email: "partner@example.com" };
const noop = () => {};

describe("the join link is shown once, and only while it is in state", () => {
  it("renders nothing for the link slot when no link is in state", () => {
    expect(inviteLinkSlot(null, noop)).toBeNull();
  });

  it("renders the block when a link is in state", () => {
    const slot = inviteLinkSlot(MINTED, noop);
    expect([...walk(slot)].some((el) => el.type === InviteLinkBlock)).toBe(true);
  });

  it("puts the link in the copy control, exactly once, and nowhere else as text", () => {
    const tree = InviteLinkBlock({ ...MINTED, onDone: noop });
    const copies = [...walk(tree)].filter((el) => el.type === CopyButton);
    expect(copies).toHaveLength(1);
    expect((copies[0].props as { value: string }).value).toBe(LINK);
    expect(strings(tree).filter((s) => s.includes("/join/"))).toEqual([]);
  });

  it("says to send it, that it works once, and who must sign in to use it", () => {
    const text = strings(InviteLinkBlock({ ...MINTED, onDone: noop })).join("");
    expect(text).toContain("Send this link to them. It works once.");
    expect(text).toContain("partner@example.com");
    expect(text).toMatch(/Google/);
  });

  it("is dismissed by its Done button", () => {
    const onDone = vi.fn();
    const done = [...walk(InviteLinkBlock({ ...MINTED, onDone }))].find(
      (el) => el.type === Button && strings(el).join("") === "Done",
    );
    (done?.props as { onClick: () => void }).onClick();
    expect(onDone).toHaveBeenCalledTimes(1);
  });
});

describe("the pending invitations an admin sees", () => {
  const ROWS: PendingInvitation[] = [
    {
      id: "77777777-7777-4777-8777-777777777777",
      email: "partner@example.com",
      role: "admin",
      expiresAt: "2026-10-12T23:30:00+00:00",
    },
    {
      id: "99999999-9999-4999-8999-999999999999",
      email: null,
      role: "member",
      expiresAt: "2026-10-13T12:00:00+00:00",
    },
  ];

  it("says the list could not be loaded, rather than showing none, when the read failed", () => {
    expect(strings(PendingInvitations({ invitations: null, tz: "UTC" })).join("")).toMatch(
      /could not be loaded/i,
    );
  });

  it("renders nothing when there are none", () => {
    expect(PendingInvitations({ invitations: [], tz: "UTC" })).toBeNull();
  });

  it("lists each invitation's address, role and expiry on the workspace's clock", () => {
    const text = strings(PendingInvitations({ invitations: ROWS, tz: "Pacific/Auckland" })).join(
      " | ",
    );
    expect(text).toContain("partner@example.com");
    // "Invited as", so a pending row cannot read as a member with that role.
    expect(text).toContain("Invited as admin · expires Oct 13");
    expect(text).toContain("Telegram invitation");
    expect(text).toContain("Invited as member · expires Oct 14");
  });
});
