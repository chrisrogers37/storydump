/**
 * The runway card, read as a returned element tree (`environment: "node"` —
 * no DOM; the component is a plain function of its rows).
 */

import { describe, expect, it } from "vitest";
import type { ReactElement, ReactNode } from "react";
import { RunwayCard } from "./runway-card";
import { EmptyState } from "./empty-state";
import { TONE_CLASS } from "./tone";
import { Badge } from "@/components/ui/badge";
import type { RunwayRow } from "@/lib/runway";

/** Depth-first walk of a returned tree, children flattened. */
function* walk(node: ReactNode): Generator<ReactElement> {
  if (node === null || node === undefined || typeof node !== "object") return;
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  const el = node as ReactElement<{ children?: ReactNode }>;
  yield el;
  yield* walk(el.props?.children);
}

/** Every string under a node, concatenated. */
function textOf(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  return textOf((node as ReactElement<{ children?: ReactNode }>).props?.children);
}

/**
 * The warning level the rows were marked at. Not the server's default, so a
 * level hardcoded in the card cannot pass for the one it was handed.
 */
const BELOW_DAYS = 5;

const ROWS: RunwayRow[] = [
  {
    key: "a1",
    name: "storyco",
    headline: "About 4 days",
    detail: "14 files at 3 a day",
    low: true,
  },
  {
    key: "a2",
    name: "second",
    headline: "About 30 days",
    detail: "90 files at 3 a day",
    low: false,
  },
];

describe("RunwayCard", () => {
  it("gives every account its line, with the days and the arithmetic", () => {
    const items = [...walk(RunwayCard({ rows: ROWS, belowDays: BELOW_DAYS }))].filter(
      (el) => el.type === "li",
    );
    expect(items.map(textOf)).toEqual([
      "storycoRunning lowAbout 4 days · 14 files at 3 a day",
      "secondAbout 30 days · 90 files at 3 a day",
    ]);
  });

  it("marks only the accounts below the warning level", () => {
    const items = [...walk(RunwayCard({ rows: ROWS, belowDays: BELOW_DAYS }))].filter(
      (el) => el.type === "li",
    );
    const marked = items.map((item) =>
      [...walk(item)].some(
        (el) =>
          el.type === Badge &&
          (el.props as { className?: string }).className === TONE_CLASS.attention,
      ),
    );
    expect(marked).toEqual([true, false]);
  });

  it("says who is told and when, in the server's own number", () => {
    const text = textOf(RunwayCard({ rows: ROWS, belowDays: BELOW_DAYS }));
    expect(text).toContain(
      "This workspace's Telegram chats are told once when an account drops below 5 days of content.",
    );
  });

  it("says there is nothing to count rather than rendering an empty list", () => {
    const tree = RunwayCard({ rows: [], belowDays: BELOW_DAYS });
    expect([...walk(tree)].some((el) => el.type === EmptyState)).toBe(true);
    expect([...walk(tree)].some((el) => el.type === "ul" || el.type === "li")).toBe(false);
  });

  it("promises no notice on a card with no account to be told about", () => {
    const text = textOf(RunwayCard({ rows: [], belowDays: BELOW_DAYS }));
    expect(text).not.toContain("told once");
  });

  it("leaves an empty card's call to action to the Overview's setup step", () => {
    const empty = [...walk(RunwayCard({ rows: [], belowDays: BELOW_DAYS }))].find(
      (el) => el.type === EmptyState,
    );
    expect(empty, "the empty card renders no EmptyState").toBeDefined();
    const props = empty?.props as { action?: unknown; description?: string };
    expect(props.action).toBeUndefined();
    expect(props.description).toBe(
      "Days of content left show here once an account is connected.",
    );
  });
});
