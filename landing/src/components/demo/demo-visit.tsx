"use client";

import { useEffect } from "react";
import { useDemo } from "@/components/demo/demo-provider";
import type { DemoPage } from "@/lib/demo/state";

/** Notes that the visitor opened a page; the end panel counts them. Renders nothing. */
export function DemoVisit({ page }: { page: DemoPage }) {
  const { visit } = useDemo();
  useEffect(() => visit(page), [visit, page]);
  return null;
}
