/**
 * Each dashboard loading placeholder renders its page's header. The pages are
 * async server components that need a session, so the page side is read from
 * its source.
 */

import { describe, expect, it } from "vitest";
import { readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import type { ReactElement, ReactNode } from "react";
import { QueueHeader, SettingsHeader } from "./page-headers";
import { TextSkeleton } from "@/components/ui/skeleton";
import QueueLoading from "@/app/(dashboard)/dashboard/queue/loading";
import SettingsLoading from "@/app/(dashboard)/dashboard/settings/loading";

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

const DASHBOARD = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../app/(dashboard)/dashboard",
);

const PAIRS = [
  { name: "Queue", loading: QueueLoading, header: QueueHeader, page: "queue/page.tsx", use: /<QueueHeader tz=\{tz\} \/>/ },
  { name: "Settings", loading: SettingsLoading, header: SettingsHeader, page: "settings/page.tsx", use: /<SettingsHeader \/>/ },
];

describe("dashboard loading placeholders render their page's header", () => {
  for (const { name, loading, header, page, use } of PAIRS) {
    it(`${name}: the placeholder and the page both render ${header.name}`, () => {
      expect([...walk(loading())].some((el) => el.type === header)).toBe(true);
      const source = readFileSync(path.join(DASHBOARD, page), "utf8");
      expect(source).toMatch(use);
    });
  }
});

describe("QueueHeader", () => {
  it("names the zone once the config has loaded", () => {
    const header = QueueHeader({ tz: "Europe/Paris" }) as ReactElement<{ description: ReactNode }>;
    expect(textOf(header.props.description)).toMatch(/Times are in Europe\/Paris\.$/);
  });

  it("holds the zone's place with an inline bar, and words for a screen reader, while loading", () => {
    const header = [...walk(QueueLoading())].find((el) => el.type === QueueHeader);
    const tz = (header as ReactElement<{ tz: ReactNode }>).props.tz;
    expect([...walk(tz)].some((el) => el.type === TextSkeleton)).toBe(true);
    expect(textOf(tz)).toBe("your time zone");
  });
});
