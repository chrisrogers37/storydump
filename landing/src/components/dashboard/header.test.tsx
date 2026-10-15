/**
 * The dashboard header's Owner link (#1723): `/ops`, for the deployment's
 * operators only. Read as a returned element tree, as `sidebar.test.tsx` reads
 * its links.
 */

import { describe, expect, it } from "vitest";
import Link from "next/link";
import { isValidElement, type ReactElement, type ReactNode } from "react";
import type { SessionUser } from "@/lib/session";
import { DashboardHeader } from "./header";

/** Every element in a returned tree, depth-first. */
function* walk(node: ReactNode): Generator<ReactElement<{ href?: string; children?: ReactNode }>> {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  if (!isValidElement<{ href?: string; children?: ReactNode }>(node)) return;
  yield node;
  yield* walk(node.props.children);
}

const links = (user: Partial<SessionUser>) =>
  [...walk(DashboardHeader({ user: { displayName: "Owner", ...user } as SessionUser }))]
    .filter((el) => el.type === Link)
    .map((el) => [el.props.href, el.props.children]);

describe("the dashboard header", () => {
  it("links an operator to the owner area", () => {
    expect(links({ isOps: true })).toEqual([
      ["/workspaces", "Switch workspace"],
      ["/ops", "Owner"],
    ]);
  });

  it("shows no Owner link to anyone else", () => {
    expect(links({ isOps: false })).toEqual([["/workspaces", "Switch workspace"]]);
  });
});
