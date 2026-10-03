import { describe, expect, it } from "vitest";
import nextConfig from "../next.config";

/**
 * A link preview (iMessage, and the other unfurlers) reads a page's `<title>`
 * and its `og:` and `twitter:` tags from the start of the HTML. React writes
 * every hoisted `<title>` and `<meta>` after the page's stylesheets
 * (react-dom-server's preamble: charset, viewport, preloads, styles, scripts,
 * then the hoisted tags), so a stylesheet inlined into the HTML puts the whole
 * of it ahead of them: about 96 KB on the home page, where iMessage showed a
 * blank card with only the title. The stylesheet stays a `<link>`.
 */
describe("the link-preview tags stay at the top of the page", () => {
  it("does not inline the stylesheet ahead of them", () => {
    expect(nextConfig.experimental?.inlineCss).not.toBe(true);
  });
});
