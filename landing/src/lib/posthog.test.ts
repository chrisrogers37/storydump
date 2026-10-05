/**
 * What the site sends to PostHog: `redact`, which every event passes through
 * on its way out, and `capture`, which loads the library on first use and
 * refuses invite pages. The library is mocked; `posthog.init`'s config is
 * pinned whole here, because a default it leaves on is data the site collects.
 */

import type { CaptureResult } from "posthog-js/dist/module.slim.no-external";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const library = vi.hoisted(() => ({ init: vi.fn(), capture: vi.fn() }));
vi.mock("posthog-js/dist/module.slim.no-external", () => ({ default: library }));

function event(properties: Record<string, unknown>, extra: Partial<CaptureResult> = {}): CaptureResult {
  return { uuid: "u", event: "$pageview", properties, ...extra } as CaptureResult;
}

/** The browser's location, which a test moves to stand for a navigation. */
const location = { href: "", pathname: "" };

function goTo(path: string) {
  location.href = `https://storydump.app${path}`;
  location.pathname = new URL(location.href).pathname;
}

async function load({ key = "phc_test", path = "/" } = {}) {
  vi.resetModules();
  vi.stubEnv("NEXT_PUBLIC_POSTHOG_KEY", key);
  goTo(path);
  vi.stubGlobal("window", { location });
  return import("./posthog");
}

/** Waits for the library's dynamic import, which resolves the module asynchronously. */
const loaded = () => vi.waitFor(() => expect(library.init).toHaveBeenCalled());
/** For a test that expects no load: long enough for one to have happened. */
const settle = () => new Promise((resolve) => setTimeout(resolve, 50));

beforeEach(() => {
  library.init.mockClear();
  library.capture.mockClear();
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("redact", () => {
  it("keeps the page's address without its query string, and its UTM tags as properties", async () => {
    const { redact } = await load();
    const out = redact(
      event({
        $current_url: "https://storydump.app/blog?utm_source=x&utm_campaign=launch&gclid=abc&email=a%40b.c#top",
      }),
    );
    expect(out?.properties).toEqual({
      $current_url: "https://storydump.app/blog",
      utm_source: "x",
      utm_campaign: "launch",
    });
  });

  it("does not overwrite a UTM tag the event already carries", async () => {
    const { redact } = await load();
    const out = redact(event({ $current_url: "https://storydump.app/?utm_source=url", utm_source: "form" }));
    expect(out?.properties.utm_source).toBe("form");
  });

  it("keeps only the referrer's site", async () => {
    const { redact } = await load();
    const out = redact(
      event({
        $current_url: "https://storydump.app/",
        $referrer: "https://storydump.app/join/token?x=1",
        $referring_domain: "storydump.app",
      }),
    );
    expect(out?.properties).toMatchObject({
      $referrer: "https://storydump.app",
      $referring_domain: "storydump.app",
    });
  });

  it("leaves a referrer that is not an address, such as $direct", async () => {
    const { redact } = await load();
    expect(redact(event({ $referrer: "$direct" }))?.properties.$referrer).toBe("$direct");
  });

  it("drops an event from an invite page whole", async () => {
    const { redact } = await load();
    expect(redact(event({ $current_url: "https://storydump.app/join/some-token" }))).toBeNull();
  });

  it("never names an invite page as the previous page", async () => {
    const { redact } = await load();
    const out = redact(event({ $current_url: "https://storydump.app/dashboard", $prev_pageview_pathname: "/join/tok" }));
    expect(out?.properties).not.toHaveProperty("$prev_pageview_pathname");
    const kept = redact(event({ $current_url: "https://storydump.app/blog", $prev_pageview_pathname: "/" }));
    expect(kept?.properties.$prev_pageview_pathname).toBe("/");
  });

  it("sends no page title and no person properties", async () => {
    const { redact } = await load();
    const out = redact(event({ title: "Storydump" }, { $set: { a: 1 }, $set_once: { b: 2 } }));
    expect(out?.properties).not.toHaveProperty("title");
    expect(out).not.toHaveProperty("$set");
    expect(out).not.toHaveProperty("$set_once");
  });
});

describe("capture", () => {
  it("loads the library once, with exactly this config", async () => {
    const { capture, redact } = await load();
    capture("CTA Click", { location: "header" });
    capture("$pageview");
    await loaded();
    expect(library.init).toHaveBeenCalledTimes(1);
    expect(library.init).toHaveBeenCalledWith("phc_test", {
      api_host: "https://us.i.posthog.com",
      cookieless_mode: "always",
      persistence: "memory",
      person_profiles: "never",
      debug: false,
      capture_pageview: false,
      autocapture: false,
      disable_session_recording: true,
      advanced_disable_flags: true,
      disable_external_dependency_loading: true,
      save_campaign_params: false,
      property_denylist: [
        "ph_keyword",
        "$screen_height",
        "$screen_width",
        "$viewport_height",
        "$viewport_width",
        "$timezone",
        "$timezone_offset",
        "$browser_language",
        "$browser_language_prefix",
      ],
      before_send: redact,
    });
  });

  it("sends calls made while the library downloads, in order, with the page and time they were made on", async () => {
    const { capture } = await load({ path: "/?utm_source=x" });
    const before = Date.now();
    capture("CTA Click", { location: "header" });
    goTo("/blog");
    capture("$pageview");
    await loaded();
    const calls = library.capture.mock.calls;
    expect(calls.map(([name]) => name)).toEqual(["CTA Click", "$pageview"]);
    expect(calls[0][1]).toEqual({
      location: "header",
      $current_url: "https://storydump.app/?utm_source=x",
      $pathname: "/",
    });
    expect(calls[1][1]).toMatchObject({ $pathname: "/blog" });
    expect(calls[0][2].timestamp.getTime()).toBeGreaterThanOrEqual(before);
  });

  it("drops a queued call if the visitor is on an invite page when the library arrives", async () => {
    const { capture } = await load();
    capture("$pageview");
    goTo("/join/some-token");
    await loaded();
    await settle();
    expect(library.capture).not.toHaveBeenCalled();
  });

  it("sends nothing, and loads nothing, on an invite page", async () => {
    const { capture } = await load({ path: "/join/some-token" });
    capture("$pageview");
    capture("Sign In Click", { location: "header" });
    await settle();
    expect(library.init).not.toHaveBeenCalled();
    expect(library.capture).not.toHaveBeenCalled();
  });

  it("sends nothing without a project key", async () => {
    const { capture } = await load({ key: "" });
    capture("$pageview");
    await settle();
    expect(library.init).not.toHaveBeenCalled();
  });
});
