/**
 * The condition panel, read as a returned element tree (this suite's
 * `environment: "node"` — no DOM; the component is a plain function of its
 * conditions, so the tree is the whole behaviour). The rule it pins is stated
 * on `ConditionsPanel`.
 */

import { describe, expect, it } from "vitest";
import Link from "next/link";
import type { ReactElement, ReactNode } from "react";
import { ConditionsPanel } from "./conditions-panel";
import {
  ALL_CLEAR_DETAIL,
  nextSetupStep,
  type Condition,
} from "@/lib/conditions";

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
    text: "2 posts need attention",
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

describe("ConditionsPanel — a workspace that is not set up yet", () => {
  const NO_INSTAGRAM = nextSetupStep({ accounts: [], sources: [] })!;
  const NO_FOLDER = nextSetupStep({ accounts: [{}], sources: [] })!;

  it("points at connecting Instagram first, never the all-clear", () => {
    const tree = ConditionsPanel({ conditions: [], setupStep: NO_INSTAGRAM });
    const text = textOf(tree);
    expect(text).toContain("Connect your Instagram account to get started");
    expect(text).toContain("step 1 of 2");
    expect(text).not.toContain("Nothing needs your attention");
    const link = [...walk(tree)].find((el) => el.type === Link);
    expect((link!.props as { href?: unknown }).href).toBe(
      "/dashboard/settings?tab=accounts",
    );
  });

  it("then at Google Drive and a folder", () => {
    const tree = ConditionsPanel({ conditions: [], setupStep: NO_FOLDER });
    const text = textOf(tree);
    expect(text).toContain("Connect Google Drive and pick a folder");
    expect(text).toContain("step 2 of 2");
    expect(text).not.toContain("Nothing needs your attention");
    const link = [...walk(tree)].find((el) => el.type === Link);
    expect((link!.props as { href?: unknown }).href).toBe(
      "/dashboard/settings?tab=integrations",
    );
  });

  it("gives a member the step with no button", () => {
    const step = nextSetupStep({ accounts: [], sources: [] }, { isAdmin: false })!;
    const tree = ConditionsPanel({ conditions: [], setupStep: step });
    expect(textOf(tree)).toContain("Waiting on an admin to connect Instagram");
    expect([...walk(tree)].find((el) => el.type === Link)).toBeUndefined();
  });

  it("a condition still wins over a setup step", () => {
    const text = textOf(
      ConditionsPanel({ conditions: CONDITIONS, setupStep: NO_FOLDER }),
    );
    expect(text).toContain("Needs your attention");
    expect(text).not.toContain("Connect Google Drive");
  });

  it("a set-up workspace keeps the all-clear", () => {
    const text = textOf(ConditionsPanel({ conditions: [], setupStep: null }));
    expect(text).toContain("Nothing needs your attention");
  });
});
