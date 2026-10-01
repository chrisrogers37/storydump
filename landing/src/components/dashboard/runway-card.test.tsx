/**
 * The runway card, read as a returned element tree (`environment: "node"` —
 * no DOM; the component is a plain function of its rows).
 */

import { describe, expect, it } from "vitest";
import type { ReactElement, ReactNode } from "react";
import { RunwayCard } from "./runway-card";
import { EmptyState } from "./empty-state";
import { RESOLVED_IN } from "@/lib/conditions";
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

const ROWS: RunwayRow[] = [
  {
    key: "a1",
    name: "storyco",
    headline: "About 6 days",
    detail: "20 files at 3 a day",
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
    const items = [...walk(RunwayCard({ rows: ROWS, belowDays: 7 }))].filter(
      (el) => el.type === "li",
    );
    expect(items.map(textOf)).toEqual([
      "storycoAbout 6 days — running low · 20 files at 3 a day",
      "secondAbout 30 days · 90 files at 3 a day",
    ]);
  });

  it("marks only the accounts below the warning level", () => {
    const items = [...walk(RunwayCard({ rows: ROWS, belowDays: 7 }))].filter(
      (el) => el.type === "li",
    );
    const marked = items.map((item) =>
      [...walk(item)].some((el) =>
        String((el.props as { className?: string }).className ?? "").includes("amber"),
      ),
    );
    expect(marked).toEqual([true, false]);
  });

  it("says when the workspace is told, in the server's own number", () => {
    const text = textOf(RunwayCard({ rows: ROWS, belowDays: 7 }));
    expect(text).toContain("You are told once when an account drops below 7 days of content.");
  });

  it("says there is nothing to count rather than rendering an empty list", () => {
    const tree = RunwayCard({ rows: [], belowDays: 7 });
    expect([...walk(tree)].some((el) => el.type === EmptyState)).toBe(true);
    expect([...walk(tree)].some((el) => el.type === "ul" || el.type === "li")).toBe(false);
  });

  it("links an empty card to Accounts, where an account is connected", () => {
    const empty = [...walk(RunwayCard({ rows: [], belowDays: 7 }))].find(
      (el) => el.type === EmptyState,
    );
    expect(empty?.props).toMatchObject({
      action: { label: RESOLVED_IN.accounts.action, href: RESOLVED_IN.accounts.href },
    });
  });
});
