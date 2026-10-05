/**
 * What the site sends to PostHog: `redact`, which every event passes through
 * on its way out, and `capture`, which loads the library on first use and
 * refuses invite pages. The library is mocked; `posthog.init`'s config is
 * pinned here, because a default it leaves on is data the site collects.
 */

import type { CaptureResult } from "posthog-js/dist/module.slim.no-external";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const library = vi.hoisted(() => ({ init: vi.fn(), capture: vi.fn() }));
vi.mock("posthog-js/dist/module.slim.no-external", () => ({ default: library }));

function event(properties: Record<string, unknown>, extra: Partial<CaptureResult> = {}): CaptureResult {
  return { uuid: "u", event: "$pageview", properties, ...extra } as CaptureResult;
}

async function load({ key = "phc_test", pathname = "/" } = {}) {
  vi.resetModules();
  vi.stubEnv("NEXT_PUBLIC_POSTHOG_KEY", key);
  vi.stubGlobal("window", { location: { pathname } });
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

  it("sets no person properties", async () => {
    const { redact } = await load();
    const out = redact(event({}, { $set: { a: 1 }, $set_once: { b: 2 } }));
    expect(out).not.toHaveProperty("$set");
    expect(out).not.toHaveProperty("$set_once");
  });
});

describe("capture", () => {
  it("loads the library once, cookieless, with every optional feature off", async () => {
    const { capture, redact } = await load();
    capture("CTA Click", { location: "header" });
    capture("$pageview");
    await loaded();
    expect(library.init).toHaveBeenCalledTimes(1);
    expect(library.init).toHaveBeenCalledWith(
      "phc_test",
      expect.objectContaining({
        api_host: "https://us.i.posthog.com",
        cookieless_mode: "always",
        person_profiles: "never",
        capture_pageview: false,
        capture_pageleave: false,
        autocapture: false,
        disable_session_recording: true,
        advanced_disable_flags: true,
        disable_external_dependency_loading: true,
        save_campaign_params: false,
        property_denylist: expect.arrayContaining(["ph_keyword", "$screen_width", "$timezone"]),
        before_send: redact,
      }),
    );
  });

  it("sends calls made while the library downloads, in order, with the time they were made", async () => {
    const { capture } = await load();
    const before = Date.now();
    capture("CTA Click", { location: "header" });
    capture("$pageview");
    await loaded();
    expect(library.capture.mock.calls.map(([name]) => name)).toEqual(["CTA Click", "$pageview"]);
    const [, props, options] = library.capture.mock.calls[0];
    expect(props).toEqual({ location: "header" });
    expect(options.timestamp.getTime()).toBeGreaterThanOrEqual(before);
  });

  it("sends nothing, and loads nothing, on an invite page", async () => {
    const { capture, capturePageview } = await load({ pathname: "/join/some-token" });
    capturePageview();
    capture("Sign In Click", { location: "header" });
    await settle();
    expect(library.init).not.toHaveBeenCalled();
    expect(library.capture).not.toHaveBeenCalled();
  });

  it("sends nothing without a project key", async () => {
    const { capturePageview } = await load({ key: "" });
    capturePageview();
    await settle();
    expect(library.init).not.toHaveBeenCalled();
  });
});
