import { describe, expect, it } from "vitest";
import { memberOrigin } from "./members";

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
