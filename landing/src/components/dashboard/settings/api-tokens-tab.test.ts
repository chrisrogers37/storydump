import { describe, expect, it } from "vitest";
import type { ReactElement, ReactNode } from "react";
import { CopyButton } from "@/components/setup/copy-button";
import type { MintedToken } from "@/lib/tokens";
import {
  MintedSecretBlock,
  SERVICE_ROLE_NOTE,
  mintFormValid,
  roleCopy,
  secretSlot,
  tokenStateBadge,
} from "./api-tokens-tab";

/**
 * The pure decisions behind Settings › API tokens, and the one render
 * contract that matters most: THE SECRET IS ON SCREEN ONLY WHILE ONE IS IN
 * STATE, and then in exactly one place.
 *
 * NO DOM, deliberately — `vitest.config.ts` pins `environment: "node"` and
 * says why. The tab itself calls hooks and cannot be invoked here; the block
 * that shows the secret is hook-free on purpose, so its element tree can be
 * read without a renderer (the `media-loading-contract` walk).
 */

/** Depth-first walk of a React element tree, children flattened. */
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

/** Every string leaf in a tree, in document order. */
function strings(node: ReactNode): string[] {
  if (typeof node === "string") return [node];
  if (typeof node === "number") return [String(node)];
  if (node === null || node === undefined || typeof node !== "object")
    return [];
  if (Array.isArray(node)) return node.flatMap(strings);
  return strings(
    (node as ReactElement<{ children?: ReactNode }>).props?.children,
  );
}

const MINTED: MintedToken = {
  id: "22222222-2222-4222-8222-222222222222",
  name: "claude-code on chris-mbp",
  role: "operator",
  expiresAt: "2026-12-14T00:00:00Z",
  secret: "sdt_" + "z".repeat(43),
  workspaceId: null,
};

const noop = () => {};

describe("the secret is shown once, and only while it is in state", () => {
  it("renders nothing for the secret slot when no secret is in state", () => {
    expect(secretSlot(null, noop)).toBeNull();
  });

  it("renders the block when a secret is in state", () => {
    const slot = secretSlot(MINTED, noop);
    expect(slot).not.toBeNull();
    expect([...walk(slot)].some((el) => el.type === MintedSecretBlock)).toBe(
      true,
    );
  });

  it("puts the secret in the copy control, exactly once, and nowhere else as text", () => {
    const tree = MintedSecretBlock({ token: MINTED, onDismiss: noop });
    const copies = [...walk(tree)].filter((el) => el.type === CopyButton);
    expect(copies).toHaveLength(1);
    expect((copies[0].props as { value: string }).value).toBe(MINTED.secret);
    // The copy control is the one carrier. A second rendering — a caption, a
    // title attribute — is a second place the secret can be scraped from.
    expect(strings(tree).filter((s) => s.includes(MINTED.secret))).toEqual([]);
  });

  it("says how to use it and that it will not be shown again", () => {
    const text = strings(
      MintedSecretBlock({ token: MINTED, onDismiss: noop }),
    ).join(" ");
    expect(text).toContain("storydump login");
    expect(text).toContain("not shown again");
  });

  it("names the token it minted, so two mints in a row cannot be confused", () => {
    const text = strings(
      MintedSecretBlock({ token: MINTED, onDismiss: noop }),
    ).join(" ");
    expect(text).toContain(MINTED.name);
  });
});

describe("the badge per row state", () => {
  it("only live is ever green", () => {
    expect(tokenStateBadge("live").tone).toBe("active");
    expect(tokenStateBadge("expired").tone).not.toBe("active");
    expect(tokenStateBadge("revoked").tone).not.toBe("active");
  });

  it("revoked and expired keep distinct labels — they do not share a cause", () => {
    expect(tokenStateBadge("revoked").label).toMatch(/revoked/i);
    expect(tokenStateBadge("expired").label).toMatch(/expired/i);
    expect(tokenStateBadge("live").label).toMatch(/live/i);
  });
});

describe("the words", () => {
  it("the role sentence says what each role may do", () => {
    expect(roleCopy("operator")).toMatch(/write/i);
    expect(roleCopy("readonly")).toMatch(/read/i);
    expect(roleCopy("readonly")).not.toMatch(/write/i);
    // An unknown role is shown as itself rather than as a guess about it.
    expect(roleCopy("weird")).toBe("weird");
  });

  it("the service identity note says reads only, and who writes", () => {
    expect(SERVICE_ROLE_NOTE).toMatch(/read only/i);
    expect(SERVICE_ROLE_NOTE).toMatch(/people with their own tokens/i);
  });
});

describe("the mint form's submit gate", () => {
  it("needs a name of 1–80 characters and an expiry of 1–365 whole days", () => {
    expect(mintFormValid({ name: "laptop", days: "90" })).toBe(true);
    expect(mintFormValid({ name: "laptop", days: "1" })).toBe(true);
    expect(mintFormValid({ name: "laptop", days: "365" })).toBe(true);
    expect(mintFormValid({ name: "", days: "90" })).toBe(false);
    expect(mintFormValid({ name: "   ", days: "90" })).toBe(false);
    expect(mintFormValid({ name: "a".repeat(81), days: "90" })).toBe(false);
    expect(mintFormValid({ name: "laptop", days: "" })).toBe(false);
    expect(mintFormValid({ name: "laptop", days: "0" })).toBe(false);
    expect(mintFormValid({ name: "laptop", days: "366" })).toBe(false);
    expect(mintFormValid({ name: "laptop", days: "1.5" })).toBe(false);
    expect(mintFormValid({ name: "laptop", days: "ninety" })).toBe(false);
  });
});
