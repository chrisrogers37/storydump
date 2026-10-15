/**
 * The claim page (#1723): a link of a claim's shape renders one form that
 * posts the token to the API, and any other renders the refusal and no form.
 * The page is an async server component, called directly and rendered to a
 * string (`environment: "node"`).
 *
 * The token's shape is the API's. TypeScript cannot see the Python pattern,
 * so the last test reads it from the API's source, as
 * `intent-states-contract.test.ts` reads the vocabulary: the two cannot drift.
 * AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP.
 */

import { readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TARGET_API_URL } from "@/lib/target-api";
import { CLAIM_COPY, CLAIM_TOKEN, INVALID_COPY } from "./content";
import ClaimPage from "./page";

/** A claim token the length the worker draws (`sessions.new_token`). */
const TOKEN = "Cl41m-t0ken_" + "x".repeat(31);

async function render(token: string): Promise<string> {
  return renderToString(await ClaimPage({ params: Promise.resolve({ token }) }));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a claim link", () => {
  it("renders one form that posts its token to the API's claim start", async () => {
    const html = await render(TOKEN);
    const forms = html.match(/<form[^>]*>/g) ?? [];
    expect(forms).toHaveLength(1);
    expect(forms[0]).toContain(`action="${TARGET_API_URL}/auth/google/claim"`);
    expect(forms[0]).toContain(`method="post"`);
    expect(html).toContain(`<input type="hidden" name="claim" value="${TOKEN}"/>`);
    expect(html).toMatch(/<button type="submit"[^>]*>.*Continue with Google<\/button>/);
    expect(html).toContain(CLAIM_COPY.title);
    expect(html).toContain(CLAIM_COPY.description);
  });

  it("says its copy word for word", () => {
    expect(`${CLAIM_COPY.title} ${CLAIM_COPY.button}.`).toBe(
      "Your spot on Storydump is ready. Continue with Google.",
    );
    expect(CLAIM_COPY.description).toBe(
      "Any Google account works, and the one you choose becomes your sign-in.",
    );
  });

  it("reads nothing from the API, so opening it spends nothing", async () => {
    const fetch = vi.fn(async () => {
      throw new Error("the claim page reads nothing");
    });
    vi.stubGlobal("fetch", fetch);
    await render(TOKEN);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("keeps the longest token the API takes", async () => {
    const longest = "x".repeat(256);
    expect(await render(longest)).toContain(`value="${longest}"`);
  });
});

describe("a link of no claim's shape", () => {
  it.each(["x".repeat(257), "has space", "slash/ed", "pad=ded", "ünï", ""])(
    "renders the refusal, a way to sign in, and no form (%s)",
    async (token) => {
      const html = await render(token);
      expect(html).toContain("This link isn&#x27;t valid");
      expect(html).toContain(`href="/login"`);
      expect(html).toContain(INVALID_COPY.link);
      expect(html).not.toContain("<form");
      expect(html).not.toContain("auth/google/claim");
    },
  );
});

describe("the token's shape", () => {
  it("is the API's pattern, anchored", () => {
    const source = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../../../../../src/services/target/signup_admissions.py",
    );
    let python: string;
    try {
      python = readFileSync(source, "utf8");
    } catch (err) {
      throw new Error(`cannot read the API's token shape at ${source}: ${err}`);
    }
    const pattern = python.match(/^_TOKEN = re\.compile\(r"([^"]+)"\)$/m)?.[1];
    expect(pattern, "the API's _TOKEN pattern").toBeDefined();
    expect(CLAIM_TOKEN.source).toBe(`^${pattern}$`);
  });
});
