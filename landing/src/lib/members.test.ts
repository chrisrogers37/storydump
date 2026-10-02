import { describe, expect, it } from "vitest";
import {
  hasActiveTelegramGroup,
  memberOrigin,
  stillInTelegramGroupCopy,
} from "./members";

describe("memberOrigin — how a person got into the workspace", () => {
  it("the owner created it; nobody invited them", () => {
    expect(memberOrigin({ role: "owner", added_by_user_id: null })).toBe(
      "Created this workspace",
    );
  });
  it("a member nobody added joined from a bound Telegram group (07 §14)", () => {
    expect(memberOrigin({ role: "member", added_by_user_id: null })).toBe(
      "Joined from a Telegram group",
    );
  });
  it("anyone with an adder was invited", () => {
    expect(memberOrigin({ role: "member", added_by_user_id: "u-1" })).toBe(
      "Invited",
    );
    expect(memberOrigin({ role: "admin", added_by_user_id: "u-1" })).toBe(
      "Invited",
    );
  });
});

describe("hasActiveTelegramGroup — whether a removed member may still be in a group", () => {
  it("an active group binding is a group", () => {
    expect(
      hasActiveTelegramGroup([{ channel: "telegram_group", state: "active" }]),
    ).toBe(true);
  });
  it("a revoked group, or only a direct chat, is not", () => {
    expect(
      hasActiveTelegramGroup([
        { channel: "telegram_group", state: "revoked" },
        { channel: "telegram_dm", state: "active" },
      ]),
    ).toBe(false);
  });
  it("no bindings, or a list that could not be read, is not", () => {
    expect(hasActiveTelegramGroup([])).toBe(false);
    expect(hasActiveTelegramGroup(null)).toBe(false);
  });
});

describe("stillInTelegramGroupCopy — the reminder after a removal", () => {
  it("names the person by email and says to remove them in Telegram", () => {
    expect(stillInTelegramGroupCopy("sam@example.com")).toBe(
      "sam@example.com is still in your Telegram group, so they'll keep seeing new Stories there. Remove them from the group in Telegram too.",
    );
  });
  it("still reads as a sentence when there is no email on file", () => {
    expect(stillInTelegramGroupCopy(null)).toMatch(
      /^This person is still in your Telegram group/,
    );
  });
});
