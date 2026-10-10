/**
 * The sample's state before the visitor's browser has the page (#1649).
 *
 * The sample is counted from now, so a copy made ahead of the visit goes
 * stale: an hourly rebuild once served scheduled posts already past. So the
 * server draws no sample at all. Rendered here as the server renders it, to a
 * string, which is all the prerendered page can ever hold.
 */

import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DemoProvider } from "./demo-provider";

describe("the sample's state, as the server renders it", () => {
  const html = renderToString(
    createElement(DemoProvider, null, createElement("p", null, "the page beneath")),
  );

  it("draws a placeholder that says what is loading", () => {
    expect(html).toContain('role="status"');
    expect(html).toContain('aria-label="Loading the sample workspace"');
  });

  it("draws no page and no story: nothing in the prerendered page can go stale", () => {
    expect(html).not.toContain("the page beneath");
    expect(html).not.toMatch(/sample-(waiting|scheduled|finished)-/);
    expect(html).not.toMatch(/\.(jpg|mp4)/);
  });
});
