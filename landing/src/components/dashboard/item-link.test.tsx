import { describe, expect, it } from "vitest";
import { isValidElement, type ReactElement } from "react";
import { ItemLink } from "./item-link";

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

type AnchorProps = { href?: string; target?: string; rel?: string; children?: unknown };

const anchors = (link: string) =>
  [...walk(ItemLink({ link }) as ReactElement)].filter((el) => el.type === "a");

describe("ItemLink: an item's link to add by hand", () => {
  it("opens an https link in a new tab and hands the site no referrer", () => {
    const [a, ...rest] = anchors("https://example.com/menu");
    expect(rest).toEqual([]);
    const props = a.props as AnchorProps;
    expect(props.href).toBe("https://example.com/menu");
    expect(props.target).toBe("_blank");
    expect(props.rel).toBe("noopener noreferrer");
    expect(text(props.children)).toBe("https://example.com/menu");
  });

  it("shows any other link as text, never as an anchor", () => {
    for (const link of ["http://example.com/menu", "javascript:alert(1)", "https://user@example.com"]) {
      expect(anchors(link), link).toEqual([]);
      expect(text(ItemLink({ link }))).toContain(link);
    }
  });

  it("says what the link is to a screen reader, beyond its icon", () => {
    expect(text(ItemLink({ link: "https://example.com/menu" }))).toMatch(
      /^Link to add by hand: https:\/\/example\.com\/menu$/,
    );
  });
});
