"use client";

import { useEffect, useRef, type ReactNode } from "react";
import { CardTitle } from "@/components/ui/card";

/** What the day view asks of an element. A test stands in for the page with these. */
interface Target {
  focus(options?: FocusOptions): void;
  scrollIntoView(options?: ScrollIntoViewOptions): void;
}

/** A day's link in the month, which may be drawn or hidden at this width. */
interface DayLink extends Target {
  isConnected: boolean;
  getClientRects(): { length: number };
}

/** The month marks the link of the day whose view is open (`ContentCalendar`). */
const OPEN_DAY_LINKS = 'a[aria-current="date"]';

/**
 * Puts a day view that has just been drawn in front of the visitor. The day
 * view is drawn after the month, and the month can be taller than the screen,
 * so without this a press on a day changes nothing the visitor can see.
 *
 * Focus goes to the title: a screen reader says which day opened, and the next
 * Tab is the day view's own. Focus moves without scrolling, and the view is
 * then scrolled by its nearest edge, so the page moves only as far as it must
 * and not at all when the view is already whole on screen.
 */
export function showOpenedDay(title: Target, view: Target) {
  title.focus({ preventScroll: true });
  view.scrollIntoView({ block: "nearest" });
}

/**
 * Hands the visitor back to the day they opened once its view is gone: focus
 * returns to the day's link, as it returns to the button that opened a dialog,
 * and the link is scrolled back into view when the shorter page has left it
 * off screen. The month draws each day twice, a list row for phones and a grid
 * cell for wider screens, so the link is the one drawn at this width.
 */
export function returnToDay(links: DayLink[]) {
  const link = links.find((l) => l.isConnected && l.getClientRects().length > 0);
  if (!link) return;
  link.focus({ preventScroll: true });
  link.scrollIntoView({ block: "nearest" });
}

/**
 * The day view's title, and the part of the day view that knows it has been
 * opened: it shows the view when its day is drawn, and hands the visitor back
 * when the view goes. A client component because that takes the page; the day
 * view around it is drawn on the server for a workspace.
 *
 * Tied to the title being drawn, not to a link being pressed, so it holds
 * however the day arrives: from the query string in the sample workspace, in
 * the server's answer for a workspace, or by a link straight to an open day.
 */
export function OpenedDayTitle({ date, children }: { date: string; children?: ReactNode }) {
  const title = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const drawn = title.current;
    if (!drawn) return;
    // Read while the day is open: once it closes, the month marks no link.
    const links = Array.from(document.querySelectorAll<HTMLElement>(OPEN_DAY_LINKS));
    showOpenedDay(drawn, drawn.closest<HTMLElement>('[data-slot="card"]') ?? drawn);
    return () => returnToDay(links);
  }, [date]);

  return (
    <CardTitle ref={title} tabIndex={-1}>
      {children}
    </CardTitle>
  );
}
