import { describe, it, expect } from "vitest";
import { isLegacyJwt } from "../middleware";

/**
 * The predicate that decides whether middleware DELETES someone's session
 * cookie. It has one safe direction and one unsafe one, so both are pinned.
 *
 * Failing to recognise a legacy JWT is harmless: the token is rejected
 * server-side anyway and the user is redirected to sign in. Wrongly recognising
 * a LIVE opaque token signs a valid user out. So the fixtures below lean on the
 * second direction.
 */
describe("isLegacyJwt", () => {
  it("recognises a real JWT shape", () => {
    // Structure only — the segments are not a real token and decode to nothing.
    expect(isLegacyJwt("eyJhbGc.eyJzdWI.c2ln")).toBe(true);
    expect(isLegacyJwt("eyJ0eXAiOiJKV1Qi.eyJ1c2VySWQiOjF9.AAAA")).toBe(true);
  });

  it("does NOT fire on anything an opaque session token could plausibly be", () => {
    // The unsafe direction. Each of these is a shape alex's minted token could
    // legitimately take; firing on any of them signs out a live session.
    for (const opaque of [
      "0123456789abcdef0123456789abcdef",              // hex
      "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",          // uuid
      "sd1_kTQ2xR9kLmPd4vN8wY3zA6bC1dE5fG7h",          // prefixed random
      "bm90LWEtand0LWp1c3QtYmFzZTY0",                  // base64, no dots
      "abc.def.ghi",                                    // two dots, wrong prefix
      "eyJ-but-only-one.dot",                           // right prefix, wrong arity
      "eyJhbGc.eyJzdWI.c2ln.extra",                     // three dots
      "",
    ]) {
      expect(isLegacyJwt(opaque), `for ${JSON.stringify(opaque)}`).toBe(false);
    }
  });

  it("needs BOTH conditions, so neither alone can trigger a deletion", () => {
    expect(isLegacyJwt("eyJsomething")).toBe(false);        // prefix, no arity
    expect(isLegacyJwt("nope.nope.nope")).toBe(false);      // arity, no prefix
    expect(isLegacyJwt("eyJa.b.c")).toBe(true);             // both
  });
});
