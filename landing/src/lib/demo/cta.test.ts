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

  it("lands on an anchor the waitlist form actually carries", () => {
    const form = readFileSync(path.join(SRC, "components", "landing", "waitlist-form.tsx"), "utf8");
    expect(form).toContain(`id="${DEMO_WAITLIST_HREF.split("#")[1]}"`);
  });
});
