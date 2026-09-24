/**
 * The condition panel, read as a returned element tree (this suite's
 * `environment: "node"` — no DOM; the component is a plain function of its
 * conditions, so the tree is the whole behaviour).
 *
 * The property under test is that the panel is always an ANSWER: an all-clear
 * that is said in words, or a line per condition that says where it is
 * resolved. An empty list is neither — it looks exactly like a panel that
 * failed to load.
 */

import { describe, expect, it } from "vitest";
import Link from "next/link";
import type { ReactElement, ReactNode } from "react";
import { ConditionsPanel } from "./conditions-panel";
import { ALL_CLEAR_DETAIL, type Condition } from "@/lib/conditions";

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

const CONDITIONS: Condition[] = [
  {
    key: "account:a1",
    text: "storyco — Reconnect needed",
    href: "/dashboard/settings?tab=accounts",
    action: "Open Accounts",
  },
  {
    key: "review_required",
    text: "2 posts need a decision",
    href: "/dashboard/queue",
    action: "Open Queue",
  },
];

describe("ConditionsPanel", () => {
  it("says the all-clear in words, and what was checked — never an empty list", () => {
    const tree = ConditionsPanel({ conditions: [] });
    const text = textOf(tree);
    expect(text).toContain("Nothing needs your attention");
    expect(text).toContain(ALL_CLEAR_DETAIL);
    expect([...walk(tree)].some((el) => el.type === "ul" || el.type === "li")).toBe(false);
  });

  it("gives every condition its own line, with a link to where it is resolved", () => {
    const tree = ConditionsPanel({ conditions: CONDITIONS });
    const items = [...walk(tree)].filter((el) => el.type === "li");
    expect(items).toHaveLength(CONDITIONS.length);
    items.forEach((item, i) => {
      const link = [...walk(item)].find((el) => el.type === Link);
      expect(link, `line ${i} has no link`).toBeDefined();
      expect((link!.props as { href?: unknown }).href).toBe(CONDITIONS[i].href);
      expect(textOf(link)).toBe(CONDITIONS[i].action);
      expect(textOf(item)).toContain(CONDITIONS[i].text);
    });
  });

  it("never shows the all-clear beside a condition", () => {
    const text = textOf(ConditionsPanel({ conditions: CONDITIONS.slice(0, 1) }));
    expect(text).toContain("Needs your attention");
    expect(text).not.toContain("Nothing needs your attention");
  });
});
