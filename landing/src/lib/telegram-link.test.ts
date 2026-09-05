/**
 * Linking a Telegram identity, browser half (#1172 clause-1 wiring; #1157).
 *
 * The link is NAVIGATED TO, so its guard sits at the navigation: only
 * `https://t.me/<bot>?start=link-…` may be opened, never an arbitrary URL the
 * proxy happened to return.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  isTelegramLink,
  requestTelegramLink,
  telegramIdentityFrom,
  telegramLinkRefusalCopy,
  telegramLinkedFrom,
  isTelegramGroupLink,
  requestTelegramGroupLink,
  telegramGroupLinkRefusalCopy,
} from "./telegram-link";

let captured: { url: string; init: RequestInit }[];

function stubFetch(body: unknown, status = 200) {
  captured = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      captured.push({ url, init });
      return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => body,
      } as unknown as Response;
    }),
  );
}

beforeEach(() => vi.unstubAllGlobals());

describe("isTelegramLink", () => {
  it("accepts a t.me deep link carrying a link- start payload", () => {
    expect(isTelegramLink("https://t.me/storydump_app_bot?start=link-abc123")).toBe(true);
  });

  it("pins the product's bot when the site knows it", () => {
    expect(
      isTelegramLink("https://t.me/storydump_app_bot?start=link-abc", "storydump_app_bot"),
    ).toBe(true);
    expect(isTelegramLink("https://t.me/evil_bot?start=link-abc", "storydump_app_bot")).toBe(
      false,
    );
    expect(isTelegramLink("https://t.me/@storydump_app_bot?start=link-abc", "storydump_app_bot")).toBe(
      false,
    );
  });

  it.each([
    "http://t.me/storydump_app_bot?start=link-abc",
    "https://t.me.evil.example/storydump_app_bot?start=link-abc",
    "https://evil-t.me/storydump_app_bot?start=link-abc",
    "https://t.me:8443/storydump_app_bot?start=link-abc",
    "https://t.me/storydump_app_bot?start=inv-abc",
    "https://t.me/storydump_app_bot",
    "not a url",
  ])("refuses %s", (value) => {
    expect(isTelegramLink(value)).toBe(false);
  });
});

describe("requestTelegramLink", () => {
  it("asks the proxy for a link and returns it with its lifetime", async () => {
    stubFetch({ link: "https://t.me/storydump_app_bot?start=link-abc", expiresInSeconds: 900 });
    const result = await requestTelegramLink();
    expect(captured[0].url).toBe("/api/me/telegram/link");
    expect(captured[0].init.method).toBe("POST");
    expect(result).toEqual({
      ok: true,
      link: "https://t.me/storydump_app_bot?start=link-abc",
      expiresInSeconds: 900,
    });
  });

  it("refuses a link that is not a Telegram deep link, at the line before navigation", async () => {
    stubFetch({ link: "https://evil.example/?start=link-abc", expiresInSeconds: 900 });
    const result = await requestTelegramLink();
    expect(result).toEqual({ ok: false, error: "malformed_link", status: 200 });
  });

  it("carries the proxy's refusal by name", async () => {
    stubFetch({ error: "unauthenticated" }, 401);
    const result = await requestTelegramLink();
    expect(result).toEqual({ ok: false, error: "unauthenticated", status: 401 });
  });

  it("reports an unreachable app as such", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    expect(await requestTelegramLink()).toEqual({ ok: false, error: "unreachable", status: 0 });
  });
});

describe("telegramLinkRefusalCopy", () => {
  it("says the bot is not set up when the API refuses 503", () => {
    expect(telegramLinkRefusalCopy("http_503")).toMatch(/not set up/i);
  });

  it("has a sentence for the unknown case that promises nothing", () => {
    expect(telegramLinkRefusalCopy("mystery")).toMatch(/nothing changed/i);
  });
});

describe("telegramLinkedFrom", () => {
  it("is true when a telegram identity is attached", () => {
    expect(telegramLinkedFrom([{ provider: "google" }, { provider: "telegram" }])).toBe(true);
  });

  it("is false without one, and for an unreadable list", () => {
    expect(telegramLinkedFrom([{ provider: "google" }])).toBe(false);
    expect(telegramLinkedFrom(undefined)).toBe(false);
    expect(telegramLinkedFrom(null)).toBe(false);
  });
});

describe("telegramIdentityFrom", () => {
  it("names the attached Telegram identity", () => {
    expect(telegramIdentityFrom([{ provider: "telegram", display_name: "Chris R" }])).toEqual({
      displayName: "Chris R",
    });
  });

  it("is linked-without-a-name when the identity carries none", () => {
    expect(telegramIdentityFrom([{ provider: "telegram" }])).toEqual({ displayName: null });
  });

  it("is null without a telegram identity or an unreadable list", () => {
    expect(telegramIdentityFrom([{ provider: "google" }])).toBeNull();
    expect(telegramIdentityFrom(undefined)).toBeNull();
  });
});

describe("isTelegramGroupLink — the group picker link, pinned to our bot", () => {
  const BOT = "storydump_app_bot";
  it("accepts the startgroup link with a bind- payload for our bot", () => {
    expect(isTelegramGroupLink(`https://t.me/${BOT}?startgroup=bind-abc`, BOT)).toBe(true);
  });
  it("refuses a link- payload, another bot, http, another host, and no payload", () => {
    expect(isTelegramGroupLink(`https://t.me/${BOT}?startgroup=link-abc`, BOT)).toBe(false);
    expect(isTelegramGroupLink("https://t.me/other_bot?startgroup=bind-abc", BOT)).toBe(false);
    expect(isTelegramGroupLink(`http://t.me/${BOT}?startgroup=bind-abc`, BOT)).toBe(false);
    expect(isTelegramGroupLink(`https://evil.example/${BOT}?startgroup=bind-abc`, BOT)).toBe(false);
    expect(isTelegramGroupLink(`https://t.me/${BOT}?startgroup=bind-`, BOT)).toBe(false);
    expect(isTelegramGroupLink(`https://t.me/${BOT}?start=bind-abc`, BOT)).toBe(false);
  });
});

describe("requestTelegramGroupLink", () => {
  const WS = "11111111-1111-4111-8111-111111111111";
  let captured: { url: string; init: RequestInit }[] = [];
  function stub(body: unknown, status = 200) {
    captured = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        captured.push({ url, init });
        return { ok: status >= 200 && status < 300, status, json: async () => body } as unknown as Response;
      }),
    );
  }
  it("asks the workspace's proxy and returns the link and its lifetime", async () => {
    stub({ link: "https://t.me/storydump_app_bot?startgroup=bind-abc", expiresInSeconds: 900 });
    const result = await requestTelegramGroupLink(WS);
    expect(captured[0].url).toBe(`/api/workspaces/${WS}/telegram/bind-link`);
    expect(captured[0].init.method).toBe("POST");
    expect(result).toEqual({
      ok: true,
      link: "https://t.me/storydump_app_bot?startgroup=bind-abc",
      expiresInSeconds: 900,
    });
  });
  it("carries the proxy's refusal by name", async () => {
    stub({ error: "insufficient_role" }, 403);
    expect(await requestTelegramGroupLink(WS)).toEqual({ ok: false, error: "insufficient_role", status: 403 });
  });
});

describe("telegramGroupLinkRefusalCopy", () => {
  it("names the admin floor on a role refusal", () => {
    expect(telegramGroupLinkRefusalCopy("insufficient_role")).toMatch(/admin/i);
    expect(telegramGroupLinkRefusalCopy("http_403")).toMatch(/admin/i);
  });
  it("says the deployment is not set up on a 503", () => {
    expect(telegramGroupLinkRefusalCopy("http_503")).toMatch(/not set up/i);
  });
});
