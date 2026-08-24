/**
 * #1016 — `hasTenant()`, the predicate the whole tenant-scoped surface routes on.
 *
 * WHY THIS EXISTS SEPARATELY FROM #1015. navi verified the cross-tenant fix by
 * measurement — real server, forged tokens, seven routes, a positive control —
 * and nothing pinned it afterwards. `landing/` had no test framework and no CI
 * job, so the "8 checks green" on those PRs never executed a line of this code.
 * Verified-once and stays-verified are different properties, and only the
 * second survives someone editing this function tomorrow.
 *
 * It is one line of pure predicate, so there is no excuse for it to be the
 * unpinned one.
 */

import { describe, expect, it } from "vitest";
import type { SessionPayload } from "./session";
import { hasTenant } from "./web-signup";

function session(over: Partial<SessionPayload> = {}): SessionPayload {
  return {
    userId: 12345,
    activeChatId: null,
    firstName: "Test",
    ...over,
  } as SessionPayload;
}

describe("hasTenant", () => {
  it("is true only when a tenant is actually selected", () => {
    expect(hasTenant(session({ activeChatId: -1001234567890 }))).toBe(true);
  });

  it("is false with no session at all", () => {
    // The unauthenticated case must not read as tenanted.
    expect(hasTenant(null)).toBe(false);
  });

  it("is false when no tenant is selected", () => {
    expect(hasTenant(session({ activeChatId: null }))).toBe(false);
  });

  it("is false for a session shape that carries no tenant field", () => {
    // A Google-rooted session has no activeChatId at all. It must report false
    // and be routed to the tenant-less surfaces — the safe direction, and the
    // behaviour SEAM 1 depends on rather than a placeholder.
    const googleish = { userId: 0, firstName: "Web" } as unknown as SessionPayload;
    expect(hasTenant(googleish)).toBe(false);
  });

  it("is false for undefined, which is not the same value as null", () => {
    // Distinct from the null case on purpose: a `!== null` check alone passes
    // undefined through, and that is the shape a new session type introduces.
    expect(hasTenant(session({ activeChatId: undefined as never }))).toBe(false);
  });

  it("does not treat chat id 0 as tenant-less", () => {
    // 0 is falsy. A truthiness check would report this session as having no
    // tenant and route a real user to the empty state — the failure mode a
    // `!== null` predicate exists to avoid, and the one a refactor reintroduces.
    expect(hasTenant(session({ activeChatId: 0 }))).toBe(true);
  });
});
