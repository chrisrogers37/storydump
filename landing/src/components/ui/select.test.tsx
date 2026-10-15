import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Select, SelectTrigger, SelectValue } from "./select";

/**
 * A select held with `aria-disabled` looks held, as a `disabled` one does
 * (#1477): a held control keeps focus only if it is not `disabled`, and one
 * that looks live invites a pick it will not take.
 */
describe("SelectTrigger", () => {
  it("looks held when held with aria-disabled, as when disabled", () => {
    const html = renderToString(
      createElement(Select, null, createElement(SelectTrigger, { "aria-disabled": true }, createElement(SelectValue))),
    );
    const classes = html.match(/<button[^>]*class="([^"]*)"/)?.[1]?.split(" ") ?? [];
    expect(classes).toContain("disabled:opacity-50");
    expect(classes).toContain("aria-disabled:opacity-50");
    expect(classes).toContain("aria-disabled:cursor-not-allowed");
    expect(html).toContain('aria-disabled="true"');
  });
});
