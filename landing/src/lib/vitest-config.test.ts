/**
 * The glob is load-bearing, so it is pinned.
 *
 * `landing/` got a test runner because CI was green over code it never
 * executed. A collection pattern that silently drops a file class recreates
 * exactly that: the run is green, rc is 0, and the uncollected file is
 * indistinguishable from a passing one. Virgil measured it — a deliberately
 * FAILING `.test.tsx` dropped in under the old pattern produced
 * `25 passed, rc 0`.
 *
 * A canary `.tsx` alone cannot protect this: if the pattern stops matching, the
 * canary stops running and says nothing. Only an assertion about the PATTERN
 * survives its own subject vanishing.
 */

import { readFileSync } from "fs";
import path from "path";
import { describe, expect, it } from "vitest";

describe("the vitest collection pattern", () => {
  const config = readFileSync(
    path.resolve(__dirname, "../../vitest.config.ts"),
    "utf8",
  );

  it("collects .tsx as well as .ts", () => {
    const include = config.match(/include:\s*\[([^\]]*)\]/)?.[1] ?? "";
    expect(include, "no include pattern found in vitest.config.ts").toBeTruthy();
    // Brace form or an explicit second entry both satisfy this; what must not
    // survive is a pattern that only reaches .ts.
    const reachesTsx = /\{ts,\s*tsx\}/.test(include) || /\.test\.tsx/.test(include);
    expect(reachesTsx, `include pattern does not reach .tsx: ${include}`).toBe(true);
  });
});
