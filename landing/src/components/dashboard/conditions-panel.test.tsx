/**
 * The condition panel, read as a returned element tree (this suite's
 * `environment: "node"` — no DOM; the component is a plain function of its
 * conditions, so the tree is the whole behaviour). The rule it pins is stated
 * on `ConditionsPanel`.
 */

import { describe, expect, it } from "vitest";
import Link from "next/link";
import { createElement, type ReactElement, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ConditionsPanel } from "./conditions-panel";
import { TONE_CLASS } from "./tone";
import {
  ALL_CLEAR_DETAIL,
  nextSetupStep,
  type Condition,
} from "@/lib/conditions";
import type { ReadinessView } from "@/lib/readiness";

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

/**
 * A converted workspace (#1644): the server's readiness, said as
 * `readinessView` built it (`readiness.test.ts` pins that), drawn here to
 * markup so the badge classes, the links and the asks are what is read.
 */
describe("ConditionsPanel — a converted workspace's readiness", () => {
  const GUIDE: ReadinessView = {
    intentional: false,
    title: "1 of 2 accounts ready",
    detail: null,
    action: null,
    lines: [
      {
        key: "b",
        name: "@example_b",
        badges: [
          { label: "No source", tone: "attention" },
          { label: "Partial coverage", tone: "attention" },
        ],
        detail: "@example_b's past posts are Off, and no Google Drive folder is on.",
        coverage: "Oldest post indexed so far: March 3, 2024.",
        action: { href: "/dashboard/settings?tab=general#posting-mix", label: "Turn on @example_b's past posts" },
        ask: null,
      },
    ],
  };

  const draw = (props: Parameters<typeof ConditionsPanel>[0]) =>
    renderToStaticMarkup(createElement(ConditionsPanel, props))
      .replace(/&#x27;/g, "'")
      .replace(/&amp;/g, "&");

  it("guides by the server's readiness instead of the all-clear or a setup step", () => {
    const html = draw({ conditions: [], readiness: GUIDE });
    expect(html).toContain(">Setup<");
    expect(html).toContain(">1 of 2 accounts ready<");
    expect(html).toContain(">@example_b<");
    expect(html).toContain("past posts are Off, and no Google Drive folder is on.");
    expect(html).toContain("Oldest post indexed so far: March 3, 2024.");
    expect(html).not.toContain("Nothing needs your attention");
    expect(html).not.toContain("step 1 of");
  });

  it("draws each badge in its tone, partial coverage included", () => {
    const html = draw({ conditions: [], readiness: GUIDE });
    for (const label of ["No source", "Partial coverage"]) {
      expect(html).toMatch(new RegExp(`class="[^"]*${TONE_CLASS.attention.replace(/[/]/g, "\\/")}[^"]*">${label}<`));
    }
    const active = draw({
      conditions: [],
      readiness: { ...GUIDE, lines: [{ ...GUIDE.lines[0], badges: [{ label: "Ready", tone: "active" }] }] },
    });
    expect(active).toMatch(new RegExp(`class="[^"]*${TONE_CLASS.active}[^"]*">Ready<`));
  });

  it("links an admin's remedy, labelled with the account", () => {
    const html = draw({ conditions: [], readiness: GUIDE });
    expect(html).toMatch(/<a[^>]*href="\/dashboard\/settings\?tab=general#posting-mix"[^>]*>Turn on @example_b's past posts<\/a>/);
  });

  it("gives a member the ask, and no button", () => {
    const member: ReadinessView = {
      ...GUIDE,
      lines: [{ ...GUIDE.lines[0], action: null, ask: "Ask an admin to enable @example_b's past posts or add a Drive folder." }],
    };
    const html = draw({ conditions: [], readiness: member });
    expect(html).toContain("Ask an admin to enable @example_b's past posts or add a Drive folder.");
    expect(html).not.toContain("<a");
  });

  it("stands beside the conditions rather than behind them", () => {
    const html = draw({ conditions: CONDITIONS, readiness: GUIDE });
    expect(html).toContain("Needs your attention");
    expect(html).toContain("2 posts need attention");
    expect(html).toContain("1 of 2 accounts ready");
  });

  it("says an intentional workspace as a choice, with its optional control and no setup eyebrow", () => {
    const html = draw({
      conditions: [],
      readiness: {
        intentional: true,
        title: "Every source is Off",
        detail: "Nothing is drawn while every source is Off.",
        action: { href: "/dashboard/settings?tab=general#posting-mix", label: "Open Posting mix" },
        lines: [],
      },
    });
    expect(html).toContain(">Every source is Off<");
    expect(html).not.toContain(">Setup<");
    expect(html).toMatch(/<a[^>]*>Open Posting mix<\/a>/);
    expect(html).not.toContain("<ul");
  });

  it("keeps the all-clear when the server has nothing to say", () => {
    expect(draw({ conditions: [], setupStep: null, readiness: null })).toContain("Nothing needs your attention");
  });
});
