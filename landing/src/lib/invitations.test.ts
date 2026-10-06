import { describe, expect, it } from "vitest";
import { expiryLabel, joinLinkFrom, pendingInvitationsFrom } from "./invitations";

/**
 * The pure decisions behind inviting a person (#1563, #1564): which link is
 * shown, which pending invitations are listed, and the date each one expires.
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
