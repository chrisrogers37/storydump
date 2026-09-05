import { describe, expect, it } from "vitest";
import { resolveEntrySession } from "./entry-session";
import { SessionUnavailableError, type SessionUser } from "./session";

const USER = { userId: "u1", displayName: "Chris" } as unknown as SessionUser;

describe("resolveEntrySession — three outcomes, told apart", () => {
  it("hands back the session when there is one", async () => {
    expect(await resolveEntrySession(async () => USER)).toEqual({
      kind: "session",
      session: USER,
    });
  });

  it("says signed out only when the answer was actually no", async () => {
    expect(await resolveEntrySession(async () => null)).toEqual({ kind: "signed_out" });
  });

  it("says unavailable — never signed out — when the API could not be asked", async () => {
    const outcome = await resolveEntrySession(async () => {
      throw new SessionUnavailableError("target_router_unreachable", 503);
    });
    expect(outcome).toEqual({ kind: "unavailable", status: 503 });
  });

  it("lets any other failure surface as the error it is", async () => {
    await expect(
      resolveEntrySession(async () => {
        throw new TypeError("programming error");
      }),
    ).rejects.toThrow(TypeError);
  });
});
