import { describe, expect, it } from "vitest";
import { httpsHref } from "./redirect-guard";

describe("httpsHref: when an item's link may become an anchor", () => {
  it("gives an https link with a host its href", () => {
    expect(httpsHref("https://example.com/menu")).toBe("https://example.com/menu");
  });

  it("reads the scheme as the browser will, whatever its case", () => {
    // The port stores the link as typed, so an upper-case scheme reaches the page.
    expect(httpsHref("HTTPS://Example.com/menu")).toBe("https://example.com/menu");
  });

  it("refuses every other scheme, so it is shown as text", () => {
    for (const link of [
      "http://example.com/menu",
      "javascript:alert(1)",
      "data:text/html,hi",
      "file:///etc/hosts",
      "mailto:a@example.com",
    ]) {
      expect(httpsHref(link), link).toBeNull();
    }
  });

  it("refuses a link that carries a user name or password", () => {
    expect(httpsHref("https://user@example.com/menu")).toBeNull();
    expect(httpsHref("https://user:secret@example.com/menu")).toBeNull();
  });

  it("refuses what does not parse as a URL, an https URL with no host among them", () => {
    for (const link of ["example.com/menu", "https://", ""]) {
      expect(httpsHref(link), link).toBeNull();
    }
  });
});
