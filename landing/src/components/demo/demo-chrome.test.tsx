/**
 * The sample workspace's chrome (#1480): the shared sidebar under its own
 * entries, the drawer that carries it below the breakpoint, and the banner.
 *
 * The drawer gets the same pins `mobile-nav.test.tsx` gives the dashboard's
 * (#1363): it hands the sidebar its `mobile` variant, and its trigger hides at
 * exactly the width the static sidebar appears. Read as returned element
 * trees, without a DOM.
 */

import { describe, expect, it, vi } from "vitest";
import Link from "next/link";
import { isValidElement, type ReactElement } from "react";

vi.mock("next/navigation", () => ({ usePathname: () => "/demo" }));

import { Sidebar } from "@/components/dashboard/sidebar";
import { DEMO_SIGN_IN_HREF, DEMO_WAITLIST_HREF } from "@/lib/demo/cta";
import { DemoCta } from "./demo-cta";
import { DemoEndPanel } from "./demo-end-panel";
import { DemoHeader } from "./demo-header";
import { DemoShell } from "./demo-shell";
import { DEMO_HOME, DEMO_NAV } from "./nav";

/** Every element in a returned tree, depth-first. */
function* walk(node: unknown): Generator<ReactElement> {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  if (!isValidElement(node)) return;
  yield node;
  yield* walk((node.props as { children?: unknown }).children);
}

/** The text a tree renders: its strings, in order. */
function text(node: unknown): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(text).join("");
  if (isValidElement(node)) return text((node.props as { children?: unknown }).children);
  return "";
}

/** The responsive prefix on one utility, `lg` of `lg:hidden`. */
function breakpointOf(className: string, utility: string): string {
  const found = className.match(new RegExp(`\\b(sm|md|lg|xl|2xl):${utility}\\b`));
  expect(found, `expected a responsive \`${utility}\` in: ${className}`).not.toBeNull();
  return found![1];
}

type SidebarProps = Parameters<typeof Sidebar>[0];

const header = () => DemoHeader() as ReactElement;

describe("the sample's drawer", () => {
  it("hands the sidebar its mobile variant and the sample's entries", () => {
    const inDrawer = [...walk(header())].find((el) => el.type === Sidebar);
    expect(inDrawer, "the drawer renders a <Sidebar>").toBeDefined();
    const props = inDrawer!.props as SidebarProps;
    expect(props.mobile).toBe(true);
    expect(props.items).toBe(DEMO_NAV);
    expect(props.home).toBe(DEMO_HOME);
  });

  it("hides its trigger at exactly the width the static sidebar appears", () => {
    const trigger = [...walk(header())].find((el) =>
      /\b(sm|md|lg|xl|2xl):hidden\b/.test(
        String((el.props as { className?: string }).className ?? ""),
      ),
    );
    expect(trigger, "the header has a breakpoint-hidden trigger").toBeDefined();

    const desktop = Sidebar({ items: DEMO_NAV, home: DEMO_HOME }) as ReactElement<{
      className: string;
    }>;
    expect(
      breakpointOf(String((trigger!.props as { className: string }).className), "hidden"),
    ).toBe(breakpointOf(desktop.props.className, "block"));
  });
});

describe("the sample's banner", () => {
  it("says what the sample is, and offers the one call to action", () => {
    const tree = header();
    expect(text(tree)).toContain(
      "Sample workspace. Nothing here is real, nothing is saved, nothing posts.",
    );
    expect([...walk(tree)].some((el) => el.type === DemoCta)).toBe(true);
  });

  it("leads to the waitlist first and Sign in second", () => {
    const hrefs = [...walk(DemoCta({}))]
      .filter((el) => el.type === Link)
      .map((el) => (el.props as { href: string }).href);
    expect(hrefs).toEqual([DEMO_WAITLIST_HREF, DEMO_SIGN_IN_HREF]);
  });
});

describe("the sample's shell", () => {
  it("draws the shared sidebar with the sample's entries, and ends its column with the end panel", () => {
    const tree = DemoShell({ children: "page" }) as ReactElement;

    const aside = [...walk(tree)].find((el) => el.type === Sidebar);
    const props = aside!.props as SidebarProps;
    expect(props.mobile).toBeUndefined();
    expect(props.items).toBe(DEMO_NAV);
    expect(props.home).toBe(DEMO_HOME);

    const main = [...walk(tree)].find((el) => el.type === "main");
    const children = (main!.props as { children: unknown[] }).children;
    expect(children[0]).toBe("page");
    expect((children[children.length - 1] as ReactElement).type).toBe(DemoEndPanel);
  });
});
