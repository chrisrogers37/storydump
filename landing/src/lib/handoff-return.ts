/**
 * The page shown again after a connect handed it to a provider.
 *
 * A connect leaves with `window.location.assign`, in the same tab, and keeps
 * its control busy while the page navigates away. The page can come back
 * still holding that state, in two ways:
 *
 * - **Back restored it from the back-forward cache.** The page returns with
 *   its memory, busy flag included, and fires `pageshow` with `persisted`.
 * - **It never left.** Something other than the tab took the address (an app
 *   that claims the provider's links), and the person came back to the tab:
 *   `visibilitychange` to "visible".
 *
 * Leaving fires neither: a navigation hides the page and never shows it, and
 * a page that is loaded afresh starts at rest anyway.
 *
 * The decision is here, with no DOM, so a test can drive it
 * (`vitest.config.ts` runs in node): a test stands in for `window` and
 * `document`.
 */

/** What this asks of the document: its visibility, and the event that says it changed. */
type VisibilityTarget = EventTarget & Pick<Document, "visibilityState">;

/**
 * Calls *reset* once, the first time the page is shown again, then stops
 * listening. Arm it right after the hand-off's `window.location.assign`;
 * the returned function stops listening sooner, as when the component that
 * armed it unmounts.
 */
export function onReturnAfterHandoff(
  reset: () => void,
  win: EventTarget = window,
  doc: VisibilityTarget = document,
): () => void {
  const onPageShow = (event: Event) => {
    if ((event as PageTransitionEvent).persisted) returned();
  };
  const onVisibilityChange = () => {
    if (doc.visibilityState === "visible") returned();
  };
  const stop = () => {
    win.removeEventListener("pageshow", onPageShow);
    doc.removeEventListener("visibilitychange", onVisibilityChange);
  };
  const returned = () => {
    stop();
    reset();
  };
  win.addEventListener("pageshow", onPageShow);
  doc.addEventListener("visibilitychange", onVisibilityChange);
  return stop;
}
