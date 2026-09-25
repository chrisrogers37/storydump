/**
 * The mobile navigation drawer (TD-D11, #1363).
 *
 * Two faults lived here at once, and each on its own made the drawer useless:
 * the drawer rendered `<Sidebar />` with no `mobile`, so it took the
 * `hidden … :block` variant and the drawer opened onto nothing; and the
 * trigger's breakpoint (`lg:hidden`) and the aside's (`md:block`) named
 * different widths, so between them the trigger and the static sidebar were
 * both on screen.
 *
 * Asserted without a DOM, per this suite's `environment: "node"` — both
 * components are read as returned element trees, which is enough because both
 * faults are props and class strings.
 */

import { describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";
import { isValidElement } from "react";

vi.mock("next/navigation", () => ({ usePathname: () => "/dashboard" }));

import { DashboardHeader } from "./header";
import { Sidebar } from "./sidebar";

/** Every element in a returned tree, depth-first. Client components are not
 *  rendered here, so their elements appear as themselves — which is the point:
 *  we want the props the drawer HANDS the sidebar, not what it renders. */
function* walk(node: unknown): Generator<ReactElement> {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  if (!isValidElement(node)) return;
  yield node;
  yield* walk((node.props as { children?: unknown }).children);
}

const header = () =>
  DashboardHeader({
    user: { email: "owner@example.com", displayName: "Owner" },
  } as Parameters<typeof DashboardHeader>[0]) as ReactElement;

/** The responsive prefix attached to one utility — `lg` of `lg:hidden`.
 *
 *  Read per-utility rather than "the only prefix in the string": the trigger
 *  is an ordinary button and the aside an ordinary column, so either may grow
 *  an unrelated `md:px-4` one day. That must not turn this into a red build
 *  about padding. */
function breakpointOf(className: string, utility: string): string {
  const found = className.match(
    new RegExp(`\\b(sm|md|lg|xl|2xl):${utility}\\b`)
  );
  expect(found, `expected a responsive \`${utility}\` in: ${className}`).not.toBeNull();
  return found![1];
}

describe("the mobile navigation drawer", () => {
  it("hands the sidebar its `mobile` variant", () => {
    // Without this the drawer renders the `hidden` variant: the sheet opens,
    // and the navigation inside it is display:none. There is no navigation at
    // all below the breakpoint.
    const inDrawer = [...walk(header())].find((el) => el.type === Sidebar);
    expect(inDrawer, "the drawer renders a <Sidebar>").toBeDefined();
    expect((inDrawer!.props as { mobile?: boolean }).mobile).toBe(true);
  });

  it("renders nothing hidden in that variant", () => {
    // The assertion the prop exists FOR. Pinning the prop alone would pass on
    // a variant that was itself hidden.
    const aside = Sidebar({ mobile: true }) as ReactElement<{ className: string }>;
    expect(aside.props.className).not.toMatch(/\bhidden\b/);
    expect(aside.props.className).not.toMatch(/:(block|flex|grid)\b/);
  });

  it("hides the trigger at exactly the width the static sidebar appears", () => {
    // The second fault. These two are complements — the drawer is the
    // navigation below the breakpoint and the aside is the navigation above it
    // — so a disagreement leaves a band showing both, or neither. Compared as
    // breakpoints rather than as literals: the value is a judgement call, the
    // AGREEMENT is the invariant.
    const trigger = [...walk(header())].find((el) =>
      /\b(sm|md|lg|xl|2xl):hidden\b/.test(
        String((el.props as { className?: string }).className ?? "")
      )
    );
    expect(trigger, "the header has a breakpoint-hidden trigger").toBeDefined();

    const desktop = Sidebar({}) as ReactElement<{ className: string }>;
    expect(desktop.props.className).toMatch(/\bhidden\b/);

    expect(
      breakpointOf(
        String((trigger!.props as { className: string }).className),
        "hidden"
      )
    ).toBe(breakpointOf(desktop.props.className, "block"));
  });
});
