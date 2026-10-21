/**
 * Scrolling to a fragment inside the dashboard's scrolling shell
 * (`fragment-scroll.ts`): the decisions, driven with no DOM. What they decide
 * shows only in a browser, which these tests cannot drive: on a document load of
 * `…#posting-mix`, the browser's own scroll leaves `main.scrollTop` at 0 and the
 * card below the fold.
 */

import { describe, expect, it, vi } from "vitest";
import { emptyLibrary } from "./past-posts-media";
import { RESOLVED_IN } from "./conditions";
import {
  POSTING_MIX_HREF,
  POSTING_MIX_ID,
  fragmentTarget,
  scrollToFragment,
  type FragmentDocument,
} from "./fragment-scroll";

/** A page with these ids drawn: each one's `scrollIntoView` is a spy, and every lookup is recorded. */
function page(ids: string[]) {
  const scrolls = new Map(ids.map((id) => [id, vi.fn()]));
  const looked: string[] = [];
  const doc: FragmentDocument = {
    getElementById(id) {
      looked.push(id);
      const scroll = scrolls.get(id);
      return scroll ? { scrollIntoView: scroll } : null;
    },
  };
  return { doc, scrolls, looked };
}

describe("the id a location hash names", () => {
  it("is the text after the #", () => {
    expect(fragmentTarget("#posting-mix")).toBe("posting-mix");
    expect(fragmentTarget(`#${POSTING_MIX_ID}`)).toBe(POSTING_MIX_ID);
  });

  it("reads an escape, as the browser's own fragment lookup does", () => {
    expect(fragmentTarget("#a%20b")).toBe("a b");
  });

  it("is nothing for no hash, a bare #, text that is not a hash, or an escape that cannot be read", () => {
    for (const hash of ["", "#", "posting-mix", "?tab=general", "#%E0%A4%A"]) {
      expect(fragmentTarget(hash), JSON.stringify(hash)).toBeNull();
    }
  });
});

describe("scrolling to the element a hash names", () => {
  it("scrolls the Posting mix card to the top of whatever scrolls it, once", () => {
    const { doc, scrolls, looked } = page([POSTING_MIX_ID, "other"]);
    expect(scrollToFragment("#posting-mix", doc)).toBe(true);
    expect(looked).toEqual(["posting-mix"]);
    expect(scrolls.get("posting-mix")).toHaveBeenCalledTimes(1);
    expect(scrolls.get("posting-mix")).toHaveBeenCalledWith({ block: "start" });
    // Only the element named: nothing else on the page moves.
    expect(scrolls.get("other")).not.toHaveBeenCalled();
  });

  it("does nothing, and asks for no element, when the address has no usable hash", () => {
    const { doc, scrolls, looked } = page([POSTING_MIX_ID]);
    for (const hash of ["", "#", "#%E0%A4%A"]) {
      expect(scrollToFragment(hash, doc), JSON.stringify(hash)).toBe(false);
    }
    expect(looked).toEqual([]);
    expect(scrolls.get(POSTING_MIX_ID)).not.toHaveBeenCalled();
  });

  it("does nothing when the page does not draw the element: another tab, or a card with no anchor", () => {
    const { doc, scrolls, looked } = page(["other"]);
    expect(scrollToFragment("#posting-mix", doc)).toBe(false);
    expect(looked).toEqual(["posting-mix"]);
    expect(scrolls.get("other")).not.toHaveBeenCalled();
    // The positive control: the same hash, on a page that draws it.
    expect(scrollToFragment("#posting-mix", page([POSTING_MIX_ID]).doc)).toBe(true);
  });
});

describe("every link to the Posting mix names the id the card carries", () => {
  it("is Settings' General tab at the card", () => {
    expect(POSTING_MIX_HREF).toBe("/dashboard/settings?tab=general#posting-mix");
    expect(POSTING_MIX_HREF.endsWith(`#${POSTING_MIX_ID}`)).toBe(true);
  });

  it("is the one the overview, the connection banner and the empty library all use", () => {
    expect(RESOLVED_IN.mix.href).toBe(POSTING_MIX_HREF);
    // The empty library's "Open Posting mix" lands on the card, not on the
    // General tab a screen above it.
    expect(emptyLibrary("on").action.href).toBe(POSTING_MIX_HREF);
    expect(emptyLibrary("off").action.href).toBe(POSTING_MIX_HREF);
  });
});
