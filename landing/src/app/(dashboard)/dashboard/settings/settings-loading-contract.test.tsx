import { describe, it, expect } from "vitest";
import { readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import type { ReactElement, ReactNode } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import SettingsLoading from "./loading";

/**
 * THE SKELETON AND THE PAGE ARE A CONTRACT — the `media-loading-contract`
 * rule, applied to the Settings tab bar.
 *
 * The skeleton reserves one pill per tab. A fourth tab (API tokens, CLI v2
 * phase 01) added to the page and not to the skeleton would show three
 * placeholders resolving into four tabs, with no way to tell design from
 * failure — the #1060 defect, one screen over. Nothing else compares the two.
 *
 * The page is an async server component that redirects without a session
 * and reads ten routes, so it is not rendered here; its tab count is read
 * from its SOURCE, which is the `destination-badge-contract` shape and has
 * the same bound — it matches text. A tab rendered from a list rather than
 * from literal `<TabsTrigger>` elements would be invisible to it, and would
 * need this test rewritten, not skipped.
 *
 * AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP.
 */

/** Depth-first walk of a React element tree, children flattened. */
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

/**
 * The tab bar is the first `flex gap-2` container in the skeleton, and the
 * pills are the `Skeleton`s directly under it. The cards below use other
 * layouts, which is what makes "first" identify the bar.
 */
function tabBarPillCount(tree: ReactNode): number {
  for (const el of walk(tree)) {
    const cls = (el.props as { className?: unknown })?.className;
    if (typeof cls === "string" && cls.includes("flex gap-2")) {
      return [...walk((el.props as { children?: ReactNode }).children)].filter(
        (c) => c.type === Skeleton,
      ).length;
    }
  }
  throw new Error(
    "no tab bar found in the settings skeleton — the layout moved, so this contract is unverified",
  );
}

const PAGE = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "page.tsx",
);

function pageTabCount(): number {
  let source: string;
  try {
    source = readFileSync(PAGE, "utf8");
  } catch (err) {
    throw new Error(
      `cannot read the settings page at ${PAGE} — the contract is unverified: ${err}`,
    );
  }
  const triggers = source.match(/<TabsTrigger\b/g) ?? [];
  if (triggers.length === 0) {
    throw new Error(
      "no <TabsTrigger> in the settings page — the tabs moved, so this contract is unverified",
    );
  }
  return triggers.length;
}

describe("the settings loading skeleton matches what the page renders", () => {
  it("reserves exactly one pill per tab", () => {
    const promised = tabBarPillCount(SettingsLoading());
    const rendered = pageTabCount();
    expect(
      promised,
      `the skeleton promises ${promised} tab pill(s) and the page renders ${rendered} tabs`,
    ).toBe(rendered);
  });

  it("includes the API tokens tab", () => {
    // The tab this contract was written for. Pinned by name so a rename or a
    // removal is a deliberate edit here rather than a silent one there.
    expect(readFileSync(PAGE, "utf8")).toMatch(/<TabsTrigger value="tokens">/);
  });
});
