/**
 * The sidebar's entries, and the one entry it matches exactly, are its
 * caller's (#1480).
 *
 * The dashboard calls it with nothing and gets its own five links, which is
 * what `mobile-nav.test.tsx` already relies on. The sample workspace hands it
 * three links under `/demo` and two labels for screens it does not have.
 * Read as returned element trees, like that file.
 */

import { describe, expect, it, vi } from "vitest";
import Link from "next/link";
import { isValidElement, type ReactElement } from "react";
import {
  CalendarDays,
  ImageIcon,
  LayoutDashboard,
  ListChecks,
  Settings,
} from "lucide-react";

const nav = vi.hoisted(() => ({ pathname: "/dashboard" }));
vi.mock("next/navigation", () => ({ usePathname: () => nav.pathname }));

import { Sidebar, type SidebarItem } from "./sidebar";

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

type LinkProps = {
  href: string;
  className?: string;
  "aria-current"?: string;
  children?: unknown;
};

/** Every link, the name first. */
const links = (tree: unknown) =>
  [...walk(tree)]
    .filter((el) => el.type === Link)
    .map((el) => el.props as LinkProps);

const SAMPLE: SidebarItem[] = [
  { href: "/demo", label: "Overview", icon: LayoutDashboard },
  { href: "/demo/queue", label: "Queue", icon: ListChecks },
  { label: "Media Library", icon: ImageIcon, note: "In your workspace" },
  { href: "/demo/calendar", label: "Calendar", icon: CalendarDays },
  { label: "Settings", icon: Settings, note: "In your workspace" },
];

const sample = (pathname: string) => {
  nav.pathname = pathname;
  return Sidebar({ items: SAMPLE, home: "/demo" }) as ReactElement;
};

/** The entries marked as the current page. */
const active = (tree: unknown) =>
  links(tree)
    .slice(1)
    .filter((l) => l["aria-current"] === "page")
    .map((l) => l.href);

describe("Sidebar", () => {
  it("is the dashboard's navigation when called with nothing", () => {
    nav.pathname = "/dashboard";
    expect(links(Sidebar({})).map((l) => l.href)).toEqual([
      "/dashboard",
      "/dashboard",
      "/dashboard/queue",
      "/dashboard/media",
      "/dashboard/media/calendar",
      "/dashboard/settings",
    ]);
  });

  it("links its name to the caller's home and draws the caller's links", () => {
    expect(links(sample("/demo")).map((l) => l.href)).toEqual([
      "/demo",
      "/demo",
      "/demo/queue",
      "/demo/calendar",
    ]);
  });

  it("matches home exactly and every other link by prefix", () => {
    expect(active(sample("/demo"))).toEqual(["/demo"]);
    expect(active(sample("/demo/queue"))).toEqual(["/demo/queue"]);
    expect(active(sample("/demo/calendar"))).toEqual(["/demo/calendar"]);
  });

  it("draws a label as text with its note, never as a link or a control", () => {
    const tree = sample("/demo");
    expect(text(tree)).toContain("Media LibraryIn your workspace");
    expect(text(tree)).toContain("SettingsIn your workspace");
    expect(links(tree).map((l) => text(l.children))).not.toContain("Media Library");
    expect([...walk(tree)].some((el) => el.type === "button")).toBe(false);
  });
});
