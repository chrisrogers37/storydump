import { readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { describe, expect, it } from "vitest";
import { DEMO_SIGN_IN_HREF, DEMO_WAITLIST_HREF } from "./cta";

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

describe("where the sample sends a visitor", () => {
  it("is the home page's waitlist, then Sign in", () => {
    expect(DEMO_WAITLIST_HREF).toBe("/#waitlist");
    expect(DEMO_SIGN_IN_HREF).toBe("/login");
  });

  it("lands on the anchor the home page's hero form carries", () => {
    const form = readFileSync(path.join(SRC, "components", "landing", "waitlist-form.tsx"), "utf8");
    // The hero form takes `waitlist`; the closing section's form takes its own
    // id, so the page has one of each. A form that drops the mapping fails here.
    const hero = form.match(/variant\s*===\s*"hero"\s*\?\s*"([^"]+)"/)?.[1];
    expect(hero).toBe(DEMO_WAITLIST_HREF.split("#")[1]);
  });
});
