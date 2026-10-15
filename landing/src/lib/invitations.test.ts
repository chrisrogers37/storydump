import { describe, expect, it } from "vitest";
import {
  askForTheLink,
  deliveryFrom,
  expiryLabel,
  invitationsForYouFrom,
  invitedLine,
  inviteLinkHeadline,
  joinLinkFrom,
  linkSentAgainCopy,
  mintedInviteFrom,
  pendingInvitationsFrom,
  whereTheLinkIs,
  type InvitationForYou,
} from "./invitations";

/**
 * The pure decisions behind inviting a person (#1563, #1564): which link is
 * shown, which pending invitations are listed, and the date each one expires.
 * And the invited person's own (115): what they are told is waiting, and what
 * "Email me the link again" answered.
 */

describe("the join link shown after an invitation is made", () => {
  it("is the link the API built, so every client shows the same one", () => {
    expect(
      joinLinkFrom({ join_url: "https://storydump.app/join/abc123", invite_token: "abc123" }),
    ).toBe("https://storydump.app/join/abc123");
  });

  it("is absent when the API built none: the web never builds one from the token", () => {
    // `join_url` is null on a deployment with no WEB_APP_URL. A link built
    // here from this page's own origin could name a host the invitation does
    // not live on, so the token is not used at all.
    for (const data of [
      { invite_token: "abc-_XYZ", join_url: null },
      { invite_token: "abc-_XYZ", join_url: "" },
      { invite_token: "abc-_XYZ" },
      { outcome: "executed" },
    ]) {
      expect(joinLinkFrom(data), JSON.stringify(data)).toBeNull();
    }
  });
});

describe("the invitation shown once, and the Revoke that takes it down", () => {
  const ID = "66666666-6666-4666-8666-666666666666";
  const LINK = "https://storydump.app/join/abc123";

  it("carries the invitation's id beside the link and the address it is for", () => {
    expect(
      mintedInviteFrom(
        {
          invitation_id: ID,
          join_url: LINK,
          invite_token: "abc123",
          delivery: { channel: "email", state: "sent" },
        },
        "partner@example.com",
      ),
    ).toEqual({
      id: ID,
      link: LINK,
      email: "partner@example.com",
      delivery: { state: "sent", reason: null },
    });
  });

  it("is absent with no link to show, or no invitation to tie it to", () => {
    for (const data of [
      { invitation_id: ID, join_url: null },
      { invitation_id: ID },
      { join_url: LINK },
      { invitation_id: "", join_url: LINK },
    ]) {
      expect(mintedInviteFrom(data, "partner@example.com"), JSON.stringify(data)).toBeNull();
    }
  });
});

describe("the pending invitations an admin is shown", () => {
  const ROW = {
    id: "77777777-7777-4777-8777-777777777777",
    delivery_channel: "email",
    email: "partner@example.com",
    role: "member",
    state: "pending",
    expires_at: "2026-10-12T20:00:00+00:00",
    invited_by_user_id: "88888888-8888-4888-8888-888888888888",
    created_at: "2026-10-05T20:00:00+00:00",
  };

  it("is null, not empty, when the read did not return a list", () => {
    for (const raw of [undefined, null, {}, "rows"]) {
      expect(pendingInvitationsFrom(raw), String(raw)).toBeNull();
    }
  });

  it("keeps the address, the role and the expiry of each row", () => {
    expect(pendingInvitationsFrom([ROW])).toEqual([
      {
        id: ROW.id,
        email: "partner@example.com",
        role: "member",
        expiresAt: "2026-10-12T20:00:00+00:00",
      },
    ]);
  });

  it("keeps a Telegram invitation, which names no address", () => {
    expect(
      pendingInvitationsFrom([{ ...ROW, delivery_channel: "telegram", email: null }]),
    ).toEqual([
      { id: ROW.id, email: null, role: "member", expiresAt: "2026-10-12T20:00:00+00:00" },
    ]);
  });

  it("drops a row it could not show rather than inventing its fields", () => {
    expect(
      pendingInvitationsFrom([ROW, { ...ROW, id: 3 }, { ...ROW, expires_at: null }, "x"]),
    ).toHaveLength(1);
  });
});

describe("the date an invitation expires, on the workspace's clock", () => {
  const AT = "2026-10-12T23:30:00+00:00";

  it("is the date in the workspace's zone, not the server's", () => {
    expect(expiryLabel(AT, "America/New_York")).toBe("Oct 12");
    expect(expiryLabel(AT, "Pacific/Auckland")).toBe("Oct 13");
  });

  it("reads Postgres's six-digit fraction", () => {
    expect(expiryLabel("2026-10-12T23:30:00.123456+00:00", "UTC")).toBe("Oct 12");
  });

  it("says UTC rather than throwing when the zone is unknown", () => {
    expect(expiryLabel(AT, "Not/AZone")).toBe("Oct 12 UTC");
  });
});

describe("what the inviter is told about the invitation's email", () => {
  it("reads the answer's delivery, and anything unknown as failed", () => {
    expect(deliveryFrom({ delivery: { channel: "email", state: "sent" } })).toEqual({
      state: "sent",
      reason: null,
    });
    expect(
      deliveryFrom({ delivery: { state: "over_budget", reason: "daily_limit" } }),
    ).toEqual({ state: "over_budget", reason: "daily_limit" });
    for (const data of [{}, { delivery: null }, { delivery: { state: "teleported" } }]) {
      expect(deliveryFrom(data), JSON.stringify(data)).toEqual({ state: "failed", reason: null });
    }
  });

  it("claims an email went out only when the provider took it", () => {
    const email = "partner@example.com";
    expect(inviteLinkHeadline({ state: "sent", reason: null }, email)).toMatch(
      /^Invitation emailed to partner@example\.com\./,
    );
    for (const [state, reason, says] of [
      ["not_configured", null, /isn't set up/],
      ["over_budget", "person_limit", /You've sent today's limit/],
      ["over_budget", "daily_limit", /Storydump has reached today's email limit/],
      ["failed", "provider_rejected", /couldn't email partner@example\.com/],
    ] as const) {
      const line = inviteLinkHeadline({ state, reason }, email);
      expect(line, `${state}/${reason}`).toMatch(says);
      expect(line, `${state}/${reason}`).not.toMatch(/emailed to/);
      expect(line, `${state}/${reason}`).toMatch(/works once/);
    }
  });
});

const ROW = {
  invitation_id: "11111111-1111-4111-8111-111111111111",
  workspace_name: "Acme",
  role: "member",
  expires_at: "2026-10-18T12:00:00.000000+00:00",
  inviter_name: "Dana",
};

describe("the invitations waiting for the signed-in person", () => {
  it("reads the API's rows, and an answer with no list as could-not-check", () => {
    expect(invitationsForYouFrom({ invitations: [ROW], resend: true })).toEqual({
      invitations: [
        {
          id: ROW.invitation_id,
          workspaceName: "Acme",
          role: "member",
          expiresAt: ROW.expires_at,
          inviterName: "Dana",
        },
      ],
      canResend: true,
    });
    expect(invitationsForYouFrom({ invitations: [] })).toEqual({ invitations: [], canResend: false });
    for (const data of [null, {}, { invitations: null }, { invitations: "x" }]) {
      expect(invitationsForYouFrom(data), JSON.stringify(data)).toBeNull();
    }
  });

  it("offers a re-send only when the API says one can send", () => {
    for (const resend of [false, "true", 1, undefined]) {
      expect(invitationsForYouFrom({ invitations: [ROW], resend })?.canResend, String(resend)).toBe(
        false,
      );
    }
  });

  it("drops a row missing what the panel shows, and reads a blank inviter as none", () => {
    const mine = invitationsForYouFrom({
      invitations: [
        { ...ROW, workspace_name: undefined },
        { ...ROW, invitation_id: "" },
        { ...ROW, inviter_name: "" },
      ],
    });
    expect(mine?.invitations).toHaveLength(1);
    expect(mine?.invitations[0].inviterName).toBeNull();
  });

  it("names who invited them and as what, or that they were invited", () => {
    const invitation = invitationsForYouFrom({ invitations: [ROW] })!.invitations[0];
    expect(invitedLine(invitation)).toBe("Dana invited you to Acme.");
    expect(invitedLine({ ...invitation, role: "admin" })).toBe("Dana invited you to Acme as an admin.");
    expect(invitedLine({ ...invitation, inviterName: null, workspaceName: " " })).toBe(
      "You're invited to a workspace.",
    );
  });

  it("says where the link is without claiming an email was sent, with the UTC date", () => {
    // Without email set up, or when an invitation's email didn't go, the inviter
    // shared the link by hand: the panel can't know an email exists.
    const invitation: InvitationForYou = invitationsForYouFrom({ invitations: [ROW] })!.invitations[0];
    expect(whereTheLinkIs(invitation)).toBe(
      "Open the invitation link you were sent to join. It expires Oct 18 (UTC).",
    );
    expect(whereTheLinkIs(invitation)).not.toMatch(/email/i);
  });

  it("names whom to ask where the link can't be sent again", () => {
    const invitation = invitationsForYouFrom({ invitations: [ROW] })!.invitations[0];
    expect(askForTheLink(invitation)).toBe("Don't have the link? Ask Dana to send it to you.");
    expect(askForTheLink({ ...invitation, inviterName: null })).toMatch(/the person who invited you/);
  });
});

describe("what Email me the link again answered", () => {
  const email = "partner@example.com";

  it("says the old link stopped only when the new one was sent", () => {
    expect(linkSentAgainCopy({ ok: true, delivery: { state: "sent", reason: null } }, email)).toBe(
      "Sent. A new link is on its way to partner@example.com, and the old one no longer works.",
    );
    for (const [state, reason, says] of [
      ["not_configured", null, /can't send email yet/],
      ["over_budget", "person_limit", /newest link you were sent still works/],
      ["over_budget", "daily_limit", /your link still works/i],
      ["failed", "provider_rejected", /the link you have still works/],
    ] as const) {
      const line = linkSentAgainCopy({ ok: true, delivery: { state, reason } }, email);
      expect(line, `${state}/${reason}`).toMatch(says);
      expect(line, `${state}/${reason}`).not.toMatch(/on its way|no longer works/);
    }
  });

  it("tells a closed invitation from a lost answer from a refusal that changed nothing", () => {
    expect(linkSentAgainCopy({ ok: false, error: "not_acceptable", status: 404 }, email)).toMatch(
      /no longer open/,
    );
    // No answer, or a server's error: the email may have gone, so it says to look.
    for (const [error, status] of [
      ["unreachable", 0],
      ["http_502", 502],
    ] as const) {
      const line = linkSentAgainCopy({ ok: false, error, status }, email);
      expect(line, error).toMatch(/couldn't confirm whether the email went/);
      expect(line, error).not.toMatch(/nothing changed/i);
    }
    expect(linkSentAgainCopy({ ok: false, error: "invalid_invitation", status: 400 }, email)).toBe(
      "Could not ask for the link. Nothing changed — try again shortly.",
    );
  });
});
