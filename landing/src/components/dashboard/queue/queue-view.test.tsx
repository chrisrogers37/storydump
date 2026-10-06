/**
 * `QueueView` offers the levers its caller hands it and adds none of its own
 * (#1480).
 *
 * The real Queue hands it the matrix; the sample workspace hands it Approve,
 * Skip and Reject, never Posted myself, because nothing in the sample may
 * look like it posted. Both lists are the caller's decision, so this pins
 * that the view decides nothing, and that the line under a row reads as what
 * it is: a refusal is an alert, what a tap did is a status. Read as returned
 * element trees, like `mobile-nav.test.tsx`.
 */

import { describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactElement } from "react";
import { Button } from "@/components/ui/button";
import { DialogDescription } from "@/components/ui/dialog";
import { EmptyState } from "@/components/dashboard/empty-state";
import type { Intent } from "@/lib/intents";
import { QueueView, type RowNote } from "./queue-view";

function intent(overrides: Partial<Intent> = {}): Intent {
  return {
    id: "row-1",
    state: "awaiting_approval",
    ig_account_id: "account-1",
    media_item_id: "media-1",
    schedule_slot_at: "2026-10-01T14:00:00+00:00",
    approval_mode: "manual",
    published_via: null,
    publish_step: null,
    cancel_requested: false,
    ig_permalink: null,
    entered_state_at: "2026-10-01T14:00:00+00:00",
    created_at: "2026-10-01T13:00:00+00:00",
    file_name: "sample.jpg",
    media_kind: "image",
    thumbnail_url: null,
    caption: null,
    category: null,
    account_handle: "example.brand",
    account_display_name: "Example Co",
    ...overrides,
  };
}

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

type ViewProps = Parameters<typeof QueueView>[0];

const view = (props: Partial<ViewProps> = {}) =>
  QueueView({
    intents: [intent()],
    tz: "UTC",
    truncatedAt: null,
    pending: null,
    actionsOf: () => [],
    noteFor: () => null,
    onAction: () => {},
    ...props,
  }) as ReactElement;

type ButtonProps = { size?: string; disabled?: boolean; onClick?: () => void; children?: unknown };

/** A row's levers: its small buttons. The dialog's own buttons are not levers. */
const levers = (tree: ReactElement) =>
  [...walk(tree)].filter(
    (el) => el.type === Button && (el.props as ButtonProps).size === "sm",
  ) as ReactElement<ButtonProps>[];

describe("QueueView", () => {
  it("offers exactly the levers its caller hands it", () => {
    const tree = view({ actionsOf: () => ["approve", "skip", "reject"] });
    expect(levers(tree).map((b) => text(b.props.children))).toEqual([
      "Approve",
      "Skip",
      "Reject",
    ]);
  });

  it("offers none to a row its caller gives none", () => {
    expect(levers(view({ actionsOf: () => [] }))).toEqual([]);
  });

  it("asks before Reject, in the product's own words", () => {
    const tree = view({ actionsOf: () => ["reject"] });
    const description = [...walk(tree)].find((el) => el.type === DialogDescription);
    expect(text(description)).toBe(
      "sample.jpg will never be offered again for example.brand. Skip instead if it should come back later.",
    );
  });

  it("hands a plain lever's tap to onAction", () => {
    const onAction = vi.fn();
    const [approve] = levers(view({ actionsOf: () => ["approve"], onAction }));
    approve.props.onClick!();
    expect(onAction).toHaveBeenCalledWith(intent(), "approve");
  });

  it("hands Reject to onAction only from the dialog's confirm", () => {
    const onAction = vi.fn();
    const tree = view({ actionsOf: () => ["reject"], onAction });

    const [trigger] = levers(tree);
    expect(trigger.props.onClick).toBeUndefined();

    const confirm = [...walk(tree)].find(
      (el) =>
        el.type === Button &&
        (el.props as { variant?: string }).variant === "destructive" &&
        (el.props as ButtonProps).size === undefined,
    ) as ReactElement<ButtonProps>;
    confirm.props.onClick!();
    expect(onAction).toHaveBeenCalledWith(intent(), "reject");
  });

  it("holds every lever while one is in flight", () => {
    const tree = view({ actionsOf: () => ["approve", "skip"], pending: "row-1" });
    expect(levers(tree).map((b) => b.props.disabled)).toEqual([true, true]);
  });

  it("reads a refusal as an alert and what a tap did as a status", () => {
    const line = (note: RowNote) =>
      [...walk(view({ noteFor: () => note }))].find(
        (el) => el.type === "p" && (el.props as { role?: string }).role !== undefined,
      ) as ReactElement<{ role: string; className: string; children: unknown }>;

    const refusal = line({ text: "Refused.", tone: "alert" });
    expect(refusal.props.role).toBe("alert");
    expect(refusal.props.className).toMatch(/\btext-destructive\b/);
    expect(text(refusal)).toBe("Refused.");

    const outcome = line({ text: "Done.", tone: "status" });
    expect(outcome.props.role).toBe("status");
    expect(outcome.props.className).not.toMatch(/\btext-destructive\b/);
  });

  it("says nothing is waiting when there are no rows", () => {
    expect(view({ intents: [] }).type).toBe(EmptyState);
  });
});
