import { readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { describe, expect, it } from "vitest";
import { passThrough } from "./route-guards";

/**
 * THE WORDS ARE THE CONTRACT. Every sentence the browser shows for a refusal
 * is chosen by a `switch` on one of these strings; a route that answers a
 * synonym falls through to "That did not work", which names no remedy. This
 * file exists so that a rename shows up as a failing test rather than as a
 * vaguer banner nobody files a bug about.
 */
describe("passThrough relays the API's own refusal", () => {
  it("keeps the reason and the status exactly", async () => {
    const res = passThrough({ error: "illegal_transition", status: 409 });
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({ error: "illegal_transition" });
  });

  it("does not re-code a 4xx the API already named", async () => {
    // The whole point: this tier has no opinion about the API's reason. A
    // guard that "normalised" one is how two tiers come to disagree about what
    // happened to a write.
    const res = passThrough({ error: "not_connected", status: 422 });
    expect(res.status).toBe(422);
    expect(await res.json()).toEqual({ error: "not_connected" });
  });
});

const GUARDS = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "./route-guards.ts",
);

describe("the guard vocabulary", () => {
  it("spells the three refusals the browser switches on", () => {
    // AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP — the
    // `intent-states-contract` rule.
    let src: string;
    try {
      src = readFileSync(GUARDS, "utf8");
    } catch (err) {
      throw new Error(`cannot read ${GUARDS}: ${err}`);
    }
    expect(src).toContain('{ error: "unauthenticated" }, { status: 401 }');
    expect(src).toContain('{ error: "invalid_workspace" }, { status: 400 }');
    expect(src).toContain('{ error: "malformed_body" }, { status: 400 }');
  });
});
