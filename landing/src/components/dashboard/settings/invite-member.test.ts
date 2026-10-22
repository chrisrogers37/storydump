import { describe, expect, it, vi } from "vitest";
import type { ReactElement, ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/setup/copy-button";
import type { PendingInvitation } from "@/lib/invitations";
import { InviteLinkBlock, InviteLinkSlot, PendingInvitations } from "./invite-member";

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
const MINTED = {
  id: "11111111-1111-4111-8111-111111111111",
  link: LINK,
  email: "partner@example.com",
  delivery: { state: "sent" as const, reason: null },
};
const noop = () => {};

const EMAIL_ROW: PendingInvitation = {
  id: "77777777-7777-4777-8777-777777777777",
  email: "partner@example.com",
  role: "admin",
  expiresAt: "2026-10-12T23:30:00+00:00",
};
const TELEGRAM_ROW: PendingInvitation = {
  id: "99999999-9999-4999-8999-999999999999",
  email: null,
  role: "member",
  expiresAt: "2026-10-13T12:00:00+00:00",
};
/** The card with no action in flight and handlers that record nothing. */
const IDLE = { onResend: noop, onRevoke: noop, busy: null };

function buttons(tree: ReactNode) {
  return [...walk(tree)].filter((el) => el.type === Button);
}

function labelled(tree: ReactNode, label: string) {
  return buttons(tree).filter((el) => strings(el).join("") === label);
}

describe("the join link is shown once, and only while it is in state", () => {
  it("renders nothing for the link slot when no link is in state", () => {
    expect(InviteLinkSlot({ minted: null, onDone: noop })).toBeNull();
  });

  it("renders the block when a link is in state", () => {
    const slot = InviteLinkSlot({ minted: MINTED, onDone: noop });
    expect([...walk(slot)].some((el) => el.type === InviteLinkBlock)).toBe(true);
  });

  it("puts the link in the copy control, exactly once, and nowhere else as text", () => {
    const tree = InviteLinkBlock({ ...MINTED, onDone: noop });
    const copies = [...walk(tree)].filter((el) => el.type === CopyButton);
    expect(copies).toHaveLength(1);
    expect((copies[0].props as { value: string }).value).toBe(LINK);
    expect(strings(tree).filter((s) => s.includes("/join/"))).toEqual([]);
  });

  it("says it was emailed, that the link works once, and who must sign in to use it", () => {
    const text = strings(InviteLinkBlock({ ...MINTED, onDone: noop })).join("");
    expect(text).toContain("Invitation emailed to partner@example.com.");
    expect(text).toContain("works once");
    expect(text).toContain("They need to sign in with Google as partner@example.com.");
  });

  it("says so when it was not emailed, and still offers the link once", () => {
    const delivery = { state: "not_configured" as const, reason: null };
    const text = strings(InviteLinkBlock({ ...MINTED, delivery, onDone: noop })).join("");
    expect(text).not.toContain("emailed to");
    expect(text).toContain("Send this link to them");
  });

  it("is dismissed by its Done button", () => {
    const onDone = vi.fn();
    const [done] = labelled(InviteLinkBlock({ ...MINTED, onDone }), "Done");
    (done.props as { onClick: () => void }).onClick();
    expect(onDone).toHaveBeenCalledTimes(1);
  });
});

describe("the pending invitations an admin sees", () => {
  const ROWS: PendingInvitation[] = [EMAIL_ROW, TELEGRAM_ROW];

  it("says the list could not be loaded, rather than showing none, when the read failed", () => {
    expect(strings(PendingInvitations({ invitations: null, tz: "UTC", ...IDLE })).join("")).toMatch(
      /could not be loaded/i,
    );
  });

  it("renders nothing when there are none", () => {
    expect(PendingInvitations({ invitations: [], tz: "UTC", ...IDLE })).toBeNull();
  });

  it("lists each invitation's address, role and expiry on the workspace's clock", () => {
    const text = strings(
      PendingInvitations({ invitations: ROWS, tz: "Pacific/Auckland", ...IDLE }),
    ).join(" | ");
    expect(text).toContain("partner@example.com");
    // "Invited as", so a pending row cannot read as a member with that role.
    expect(text).toContain("Invited as admin · expires Oct 13");
    expect(text).toContain("Telegram invitation");
    expect(text).toContain("Invited as member · expires Oct 14");
  });
});

describe("an admin resends or revokes a pending invitation from its row", () => {
  const BOTH = [EMAIL_ROW, TELEGRAM_ROW];

  it("gives an email invitation Resend and Revoke, and a Telegram one Revoke only", () => {
    const tree = PendingInvitations({ invitations: BOTH, tz: "UTC", ...IDLE });
    expect(labelled(tree, "Resend")).toHaveLength(1);
    expect(labelled(tree, "Revoke")).toHaveLength(2);
  });

  it("hands each button its own row", () => {
    const onResend = vi.fn();
    const onRevoke = vi.fn();
    const tree = PendingInvitations({ invitations: BOTH, tz: "UTC", onResend, onRevoke, busy: null });
    (labelled(tree, "Resend")[0].props as { onClick: () => void }).onClick();
    for (const el of labelled(tree, "Revoke")) {
      (el.props as { onClick: () => void }).onClick();
    }
    expect(onResend.mock.calls).toEqual([[EMAIL_ROW]]);
    expect(onRevoke.mock.calls).toEqual([[EMAIL_ROW], [TELEGRAM_ROW]]);
  });

  it("holds every row's buttons while a row's action is in flight, and names it on that row", () => {
    const tree = PendingInvitations({
      invitations: BOTH,
      tz: "UTC",
      ...IDLE,
      busy: { action: "revoke", id: EMAIL_ROW.id },
    });
    for (const el of buttons(tree)) {
      expect((el.props as { disabled: boolean }).disabled).toBe(true);
    }
    expect(labelled(tree, "Revoking…")).toHaveLength(1);
    expect(labelled(tree, "Resend")).toHaveLength(1);
  });

  it("holds them too while the form invites or the list re-reads, naming no row", () => {
    for (const busy of [{ action: "invite" as const }, { action: "refresh" as const }]) {
      const tree = PendingInvitations({ invitations: BOTH, tz: "UTC", ...IDLE, busy });
      for (const el of buttons(tree)) {
        expect((el.props as { disabled: boolean }).disabled, busy.action).toBe(true);
      }
      expect(labelled(tree, "Resend"), busy.action).toHaveLength(1);
      expect(labelled(tree, "Revoke"), busy.action).toHaveLength(2);
    }
  });
});
