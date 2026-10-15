"use client";

import { useEffect } from "react";
import { scrollToFragment } from "@/lib/fragment-scroll";

/**
 * Scrolls to the address's fragment once, after the page has mounted: the
 * dashboard scrolls inside `<main>`, where the browser's own fragment scroll
 * does not reach on a document load (`lib/fragment-scroll.ts` has the measure
 * and the reason). Draws nothing. Mount it on a page that has anchors.
 *
 * Scrolling only: focus is left where the page put it.
 */
export function ScrollToHash() {
  useEffect(() => {
    scrollToFragment(window.location.hash, document);
  }, []);
  return null;
}
