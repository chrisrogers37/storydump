/**
 * Scrolling to a fragment of the dashboard's own page.
 *
 * `(dashboard)/layout.tsx` is `h-screen` with an inner `<main overflow-y-auto>`,
 * so the document never scrolls: the page scrolls inside `main`. A link to
 * `…#posting-mix` that LOADS the page (the connection banner's, the overview's
 * remedies) leaves it where it was, with the card below the fold. The browser's
 * own fragment scroll runs against the document while the page is still
 * arriving and does not reach an element inside `main`. The same fragment set
 * on a page that is already loaded does scroll, since that asks for the element
 * itself.
 *
 * So the page asks for it once, after it mounts: `ScrollToHash` hands the
 * address's fragment to `scrollToFragment`, and `scrollIntoView` scrolls every
 * scrolling ancestor, `main` included. Navigation stays a plain anchor: Settings
 * opens its tab from `?tab=` on first load only, so a client-side navigation
 * would leave the tab it was on.
 *
 * The decisions are here, with no DOM, so a test can drive them
 * (`vitest.config.ts` runs in node).
 */

/** The id the Posting mix card carries on Settings › General: what its links name. */
export const POSTING_MIX_ID = "posting-mix";

/** Where the Posting mix is: Settings' General tab, at the card. One definition for every link to it. */
export const POSTING_MIX_HREF = `/dashboard/settings?tab=general#${POSTING_MIX_ID}`;

/**
 * The id a location hash names, or null for none: no hash, a bare "#", a hash
 * that does not start with one, or an escape that cannot be read.
 */
export function fragmentTarget(hash: string): string | null {
  if (!hash.startsWith("#")) return null;
  let id: string;
  try {
    id = decodeURIComponent(hash.slice(1));
  } catch {
    return null;
  }
  return id === "" ? null : id;
}

/** What this asks of the page: an element by id, and of it a scroll. A test stands in for `document`. */
export interface FragmentDocument {
  getElementById(id: string): { scrollIntoView(options?: ScrollIntoViewOptions): void } | null;
}

/**
 * Scrolls the element the hash names to the top of whatever scrolls it, which
 * its own `scroll-margin` then offsets. Returns whether there was one: a hash
 * that names nothing on this page, or a page that does not draw the element
 * (another tab, or a protocol whose card has no anchor), changes nothing.
 */
export function scrollToFragment(hash: string, doc: FragmentDocument): boolean {
  const id = fragmentTarget(hash);
  if (id === null) return false;
  const element = doc.getElementById(id);
  if (!element) return false;
  element.scrollIntoView({ block: "start" });
  return true;
}
