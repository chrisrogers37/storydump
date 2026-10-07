/**
 * The site's analytics events: what `trackEvent` sends, where it refuses to
 * send, and which names and properties the code uses.
 *
 * The event names are what the owner's PostHog insights and funnels match,
 * spelled exactly, so a renamed event is a step that silently stops
 * counting: the names are pinned here. No property may
 * carry a person's details; every value comes from a fixed list in the code.
 */

import { readdirSync, readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { describe, expect, it, vi } from "vitest";
import {
  CTA_LOCATIONS,
  DEMO_ACTIONS,
  SAMPLE_WORKSPACE_LOCATIONS,
  SIGN_IN_LOCATIONS,
  trackEvent,
} from "./analytics";
import { capture } from "./posthog";

vi.mock("./posthog", () => ({ capture: vi.fn() }));

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

describe("trackEvent", () => {
  it("sends the event with its properties", () => {
    trackEvent("CTA Click", { location: "blog_post" });
    expect(capture).toHaveBeenCalledWith("CTA Click", { location: "blog_post" });
  });
});

/** Every non-test source file outside the signed-in app. */
function publicSources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      return entry.name === "(dashboard)" || entry.name === "dashboard" ? [] : publicSources(full);
    }
    return /\.tsx?$/.test(entry.name) && !entry.name.includes(".test.") ? [full] : [];
  });
}

const SOURCES = publicSources(SRC).map((file) => readFileSync(file, "utf8"));

/** `trackEvent("Name", { … })` and `{ event: "Name", props: { … } }`. */
const CALL = /trackEvent\(\s*"([^"]+)",\s*\{([^}]*)\}|event:\s*"([^"]+)",\s*props:\s*\{([^}]*)\}/g;

const uses = SOURCES.flatMap((source) =>
  [...source.matchAll(CALL)].map((m) => ({
    name: m[1] ?? m[3],
    keys: [...(m[2] ?? m[4]).matchAll(/(\w+)\s*(?::|,|$)/g)].map((k) => k[1]),
  })),
);

describe("the events the site sends", () => {
  it("are the eight goals, spelled as the PR's manual steps spell them", () => {
    expect(new Set(uses.map((u) => u.name))).toEqual(
      new Set([
        "Waitlist Signup",
        "Waitlist Error",
        "Waitlist Start",
        "FAQ Expanded",
        "CTA Click",
        "Sign In Click",
        "Sample Workspace Click",
        "Demo Tap",
      ]),
    );
  });

  it("never carry a property that could name a person", () => {
    const keys = uses.flatMap((u) => u.keys);
    expect(keys.filter((key) => /email|name|token|user|(^|_)id($|_)|phone/i.test(key))).toEqual([]);
  });

  it("take their locations and demo actions from fixed lists", () => {
    expect(CTA_LOCATIONS).toEqual(["header", "blog_post", "use_case"]);
    expect(SIGN_IN_LOCATIONS).toEqual(["header", "hero", "closing", "footer"]);
    expect(SAMPLE_WORKSPACE_LOCATIONS).toEqual(["header", "hero"]);
    expect(DEMO_ACTIONS).toEqual([
      "post_now",
      "posted_myself",
      "skip",
      "reject",
      "open_instagram",
      "another",
    ]);
  });

  it("track every listed location somewhere", () => {
    const all = SOURCES.join("\n");
    const pairs = [
      ...SIGN_IN_LOCATIONS.map((location) => ["Sign In Click", location]),
      ...CTA_LOCATIONS.map((location) => ["CTA Click", location]),
      ...SAMPLE_WORKSPACE_LOCATIONS.map((location) => ["Sample Workspace Click", location]),
    ];
    for (const [event, location] of pairs) {
      expect(all).toMatch(new RegExp(`event: "${event}", props: \\{ location: "${location}" \\}`));
    }
  });
});
