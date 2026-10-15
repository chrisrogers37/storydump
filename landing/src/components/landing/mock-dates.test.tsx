/**
 * The landing's mocks never call a fixed date today.
 *
 * The hero's chat and the Queue under "Where the tap happens" each show a
 * card or a row with a slot, and the slot is a fixed example date. A label
 * beside it that says "Today" is true on one day of the year. So the chat is
 * headed by its card's own day, taken from the slot, and the Queue mock has
 * no such label.
 *
 * Rendered to strings, which is as far as this suite's `environment: "node"`
 * goes: it shows what each mock says.
 */

import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DEMO_DAY, DEMO_SLOT } from "./card-labels";
import { TapDemo } from "./tap-demo";
import { WhereTap } from "./where-tap";

/** How many elements hold exactly this text. */
const count = (html: string, text: string) => html.split(`>${text}<`).length - 1;

describe("the landing's mocks and their dates", () => {
  it("names the demo slot's own day", () => {
    const day = new Date(`${DEMO_SLOT.slice(0, 10)}T00:00:00Z`).toLocaleDateString("en-US", {
      month: "long",
      day: "numeric",
      timeZone: "UTC",
    });
    expect(DEMO_DAY).toBe(day);
  });

  it("heads the hero's chat with its card's day, not with Today", () => {
    const html = renderToString(createElement(TapDemo));
    expect(html).toContain(DEMO_SLOT);
    expect(count(html, DEMO_DAY)).toBe(1);
    expect(count(html, "Today")).toBe(0);
  });

  it("labels nothing Today beside the Queue mock's fixed slot", () => {
    const html = renderToString(createElement(WhereTap));
    expect(html).toContain("Fri, Oct 2, 1:30 PM");
    expect(count(html, "Today")).toBe(0);
  });
});
