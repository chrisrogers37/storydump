/**
 * THE DASHBOARD SCROLLS INSIDE ITS FRAME, NEVER THE WINDOW.
 *
 * The dashboard layout is one screen tall (`h-screen`) and only its <main>
 * scrolls. An element absolutely positioned inside <main> takes its nearest
 * positioned ancestor as its containing block; with none, it takes the page.
 * It is then laid out where it falls in <main>'s content but outside <main>'s
 * clip, and the window grows to reach it. On Settings › General the native
 * <select> that Radix renders for a Select inside a <form> (the invite form's
 * role) sat 2,152px down an 877px window: a scrollbar onto blank page below
 * the frame. `sr-only` text far down a page does the same.
 *
 * `relative` on <main> makes it that containing block, so such an element is
 * clipped and scrolled with the content it belongs to.
 *
 * WHAT THIS CANNOT SEE. It matches text and has no layout. It does not see a
 * page that opens a scroll container of its own without `relative`. `fixed`
 * is out of its scope: the viewport is its containing block either way.
 *
 * AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP.
 */

import { readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { describe, expect, it } from "vitest";

const LAYOUT = path.join(path.dirname(fileURLToPath(import.meta.url)), "layout.tsx");

/** The class list of the layout's one <main>, read from its source. */
function mainClasses(source: string): string[] {
  const mains = [...source.matchAll(/<main\b[^>]*\bclassName="([^"]*)"/g)];
  expect(mains, "the dashboard layout has exactly one <main> with a literal className").toHaveLength(1);
  return mains[0][1].split(/\s+/).filter(Boolean);
}

describe("dashboard layout: the scrolling <main> is a containing block", () => {
  const source = readFileSync(LAYOUT, "utf8");

  it("sits in a frame one screen tall", () => {
    expect(source).toMatch(/className="[^"]*\bh-screen\b[^"]*"/);
  });

  it("is the element that scrolls", () => {
    expect(mainClasses(source)).toContain("overflow-y-auto");
  });

  it("is positioned, so what is absolutely positioned inside it stays inside it", () => {
    expect(mainClasses(source)).toContain("relative");
  });
});
