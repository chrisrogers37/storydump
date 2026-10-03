/**
 * The site's analytics events: what `trackEvent` sends, where it refuses to
 * send, and which names and properties the code uses.
 *
 * The event names are Plausible goals the owner creates by hand, spelled
 * exactly (the PR's manual steps list them), so a renamed event is a goal
 * that silently stops counting: the names are pinned here. No property may
 * carry a person's details; every value comes from a fixed list in the code.
 */

import { readdirSync, readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CTA_LOCATIONS, DEMO_ACTIONS, SIGN_IN_LOCATIONS, trackEvent } from "./analytics";

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function stubWindow(pathname: string) {
  const plausible = vi.fn();
  vi.stubGlobal("window", { plausible, location: { pathname } });
  return plausible;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("trackEvent", () => {
  it("sends the event with its properties", () => {
    const plausible = stubWindow("/blog");
    trackEvent("CTA Click", { location: "blog_post" });
    expect(plausible).toHaveBeenCalledWith("CTA Click", { props: { location: "blog_post" } });
  });

  it("queues an event fired before the script loads, for the script to send", () => {
    const win: { location: { pathname: string }; plausible?: { q?: unknown[] } } = {
      location: { pathname: "/" },
    };
    vi.stubGlobal("window", win);
    trackEvent("Waitlist Start", { variant: "hero" });
    expect(win.plausible?.q).toEqual([["Waitlist Start", { props: { variant: "hero" } }]]);
  });

  it("sends nothing under /join/, where the path is an invitation token", () => {
    const plausible = stubWindow("/join/some-token/anything");
    trackEvent("Sign In Click", { location: "header" });
    expect(plausible).not.toHaveBeenCalled();

    const bare: { location: { pathname: string }; plausible?: unknown } = {
      location: { pathname: "/join/some-token" },
    };
    vi.stubGlobal("window", bare);
    trackEvent("Sign In Click", { location: "header" });
    expect(bare.plausible).toBeUndefined();
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
  it("are the seven goals, spelled as the PR's manual steps spell them", () => {
    expect(new Set(uses.map((u) => u.name))).toEqual(
      new Set([
        "Waitlist Signup",
        "Waitlist Error",
        "Waitlist Start",
        "FAQ Expanded",
        "CTA Click",
        "Sign In Click",
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
    expect(DEMO_ACTIONS).toEqual([
      "post_now",
      "posted_myself",
      "skip",
      "reject",
      "open_instagram",
      "another",
    ]);
  });

  it("track every Sign in location and every CTA location somewhere", () => {
    const all = SOURCES.join("\n");
    const pairs = [
      ...SIGN_IN_LOCATIONS.map((location) => ["Sign In Click", location]),
      ...CTA_LOCATIONS.map((location) => ["CTA Click", location]),
    ];
    for (const [event, location] of pairs) {
      expect(all).toMatch(new RegExp(`event: "${event}", props: \\{ location: "${location}" \\}`));
    }
  });
});
