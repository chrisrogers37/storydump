/**
 * The badge that told the first real user an unauthorised folder was connected.
 *
 * `media_sources.state` is `NOT NULL DEFAULT 'active'` (migration 054), the
 * screen coloured `state === "active"` green, and so a folder added seconds
 * earlier with no Google consent rendered as connected. A wrong signal in the
 * REASSURING direction is worse than no signal: the empty library then reads as
 * a sync delay rather than an unfinished setup.
 */

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import {
  sourceCredentialBadge,
  type CredentialStatus,
} from "./source-credential";

const ALL: CredentialStatus[] = ["none", "active", "expired", "revoked"];

describe("only a real credential is ever green", () => {
  it("gives the active tone to `active` and to nothing else", () => {
    // THE invariant. Everything else in this file is detail.
    for (const s of ALL) {
      expect(sourceCredentialBadge(s).tone === "active").toBe(s === "active");
    }
  });

  it("does not go green on an unknown value", () => {
    for (const s of ["", "pending", "connected", null, undefined, "ACTIVE"]) {
      expect(sourceCredentialBadge(s as string).tone).not.toBe("active");
    }
  });
});

describe("the four values stay four", () => {
  it("says something different for each — they are not a boolean", () => {
    const labels = ALL.map((s) => sourceCredentialBadge(s).label);
    expect(new Set(labels).size).toBe(ALL.length);
  });

  it("distinguishes never-connected from lost-credential", () => {
    // `none` is CONNECT; `expired` and `revoked` are RECONNECT. Different user
    // actions, which is the distinction the screen could not previously make.
    expect(sourceCredentialBadge("none").label).toMatch(/awaiting/i);
    expect(sourceCredentialBadge("expired").label).toMatch(/reconnect/i);
    expect(sourceCredentialBadge("revoked").label).toMatch(/reconnect/i);
    expect(sourceCredentialBadge("expired").label).not.toBe(
      sourceCredentialBadge("revoked").label,
    );
  });

  it("names the two states Chris could not tell apart", () => {
    expect(sourceCredentialBadge("none").label).not.toBe(
      sourceCredentialBadge("active").label,
    );
  });

  it("never renders an empty label — an absent badge is also ambiguous", () => {
    for (const s of [...ALL, "nonsense", null]) {
      expect(sourceCredentialBadge(s as string).label.trim().length).toBeGreaterThan(0);
    }
  });
});

describe("the screen no longer colours `state` green", () => {
  const src = readFileSync(
    new URL("../components/dashboard/settings/integrations-tab.tsx", import.meta.url),
    "utf8",
  );

  it("attaches green to the credential tone, not to source.state", () => {
    // The exact expression that produced the defect. If it returns, the badge
    // is claiming "connected" from a column that cannot know.
    expect(src).not.toMatch(
      /source\.state === "active"\s*\?\s*"bg-green/,
    );
    expect(src).toMatch(/cred\.tone === "active"\s*\n?\s*\?\s*"bg-green/);
  });
});
