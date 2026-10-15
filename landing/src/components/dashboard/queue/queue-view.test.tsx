/**
 * `QueueView` offers the levers its caller hands it and adds none of its own
 * (#1480).
 *
 * The real Queue hands it the matrix; the sample workspace hands it a new
 * workspace's levers (Posted myself, Skip and Reject) and answers them in the
 * browser. Both lists are the caller's decision, so this pins
 * that the view decides nothing, and that the line under a row reads as what
 * it is: a refusal is an alert, what a tap did is a status. Read as returned
 * element trees, like `mobile-nav.test.tsx`.
 */

import { describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactElement } from "react";
import { ImageIcon, Video } from "lucide-react";
import { MediaThumbnail } from "@/components/dashboard/media/media-thumbnail";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DialogDescription } from "@/components/ui/dialog";
import { EmptyState } from "@/components/dashboard/empty-state";
import { ItemLink } from "@/components/dashboard/item-link";
import { formatSlot, type Intent } from "@/lib/intents";
import { QueueView, type RowNote } from "./queue-view";
import { RescheduleDialog } from "./reschedule-dialog";

function intent(overrides: Partial<Intent> = {}): Intent {
  return {
    id: "row-1",
    state: "awaiting_approval",
    ig_account_id: "account-1",
    media_item_id: "media-1",
    schedule_slot_at: "2026-10-01T14:00:00+00:00",
    approval_mode: "manual",
    approval_rule: null,
    published_via: null,
    publish_step: null,
    cancel_requested: false,
    ig_permalink: null,
    entered_state_at: "2026-10-01T14:00:00+00:00",
    created_at: "2026-10-01T13:00:00+00:00",
    origin: "cadence",
    scheduled_by_user_id: null,
    scheduled_by: null,
    tz: "UTC",
    miss_reason: null,
    repeat: null,
    link_url: null,
    file_name: "sample.jpg",
    media_kind: "image",
    has_thumbnail: false,
    thumbnail_version: "0123456789abcdef",
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
    workspaceId: null,
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

/** A question's answers: its dialog's buttons, but Keep it. */
const answers = (tree: ReactElement) =>
  [...walk(tree)].filter(
    (el) =>
      el.type === Button &&
      (el.props as ButtonProps).size === undefined &&
      text((el.props as ButtonProps).children) !== "Keep it",
  ) as ReactElement<ButtonProps>[];

/** What each row's badges say. */
const badges = (tree: ReactElement) =>
  [...walk(tree)].filter((el) => el.type === Badge).map((el) => text(el));

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

  it("keeps a slot's date and time on one line, so a narrow row cannot break mid-date", () => {
    const tree = view({ intents: [intent({ category: "Events" })] });
    const when = [...walk(tree)].find(
      (el) => el.type === "span" && text(el) === formatSlot("2026-10-01T14:00:00+00:00", "UTC"),
    );
    expect(when, "the slot's date and time are one element").toBeDefined();
    expect(String((when!.props as { className?: string }).className)).toContain("whitespace-nowrap");
  });

  it("says nothing is waiting when there are no rows", () => {
    expect(view({ intents: [] }).type).toBe(EmptyState);
  });
});

describe("QueueView: a planned story (#1413)", () => {
  it("says who planned it, and marks a cadence story not at all", () => {
    const planned = view({ intents: [intent({ origin: "planned", scheduled_by: "Robin" })] });
    expect(badges(planned)).toContain("Planned by Robin");

    // A planned story whose person was deleted still reads as planned.
    const orphan = view({ intents: [intent({ origin: "planned", scheduled_by: null })] });
    expect(badges(orphan)).toContain("Planned");

    expect(badges(view()).some((b) => b.startsWith("Planned"))).toBe(false);
  });

  it("hands Reschedule to its dialog, with the caller's mover", () => {
    const onReschedule = vi.fn(async () => null);
    const row = intent({ origin: "planned", state: "scheduled" });
    const tree = view({ intents: [row], actionsOf: () => ["reschedule"], onReschedule });

    const dialog = [...walk(tree)].find((el) => el.type === RescheduleDialog) as ReactElement<
      Parameters<typeof RescheduleDialog>[0]
    >;
    expect(dialog.props.intent).toBe(row);
    expect(dialog.props.onSubmit).toBe(onReschedule);
    expect(dialog.props.label).toBe("Reschedule…");
    expect(dialog.props.disabled).toBe(false);
  });

  it("offers no Reschedule to a caller that cannot move a story", () => {
    // The sample workspace passes no mover, whatever a row would allow.
    const tree = view({ actionsOf: () => ["reschedule"] });
    expect([...walk(tree)].some((el) => el.type === RescheduleDialog)).toBe(false);
    expect(levers(tree)).toEqual([]);
  });

  it("asks before Cancel, which is not permanent, so its button is not red", () => {
    const onAction = vi.fn();
    const tree = view({ actionsOf: () => ["cancel"], onAction });

    const [trigger] = levers(tree);
    expect(text(trigger.props.children)).toBe("Cancel");
    expect((trigger.props as { variant?: string }).variant).toBe("outline");

    const description = [...walk(tree)].find((el) => el.type === DialogDescription);
    expect(text(description)).toBe(
      "sample.jpg won't post to example.brand. The item stays in your Media library.",
    );

    const confirm = [...walk(tree)].find(
      (el) =>
        el.type === Button &&
        (el.props as { variant?: string }).variant === "destructive" &&
        (el.props as ButtonProps).size === undefined,
    ) as ReactElement<ButtonProps>;
    expect(text(confirm.props.children)).toBe("Cancel story");
    confirm.props.onClick!();
    expect(onAction).toHaveBeenCalledWith(intent(), "cancel");
  });
});

describe("QueueView: the by-line of a story a rule approved (#1477)", () => {
  it("says which rule approved it, beside its badge", () => {
    const tree = view({ intents: [intent({ state: "approved", approval_rule: "auto" })] });
    expect(badges(tree)).toEqual(["approved", "Approved by YOLO"]);
  });

  it("keeps saying so after the approval, while the story publishes", () => {
    const tree = view({ intents: [intent({ state: "publishing", approval_rule: "item" })] });
    expect(badges(tree)).toContain("Approved by Auto-approve");
  });

  it("says nothing new for a story a person approved", () => {
    const tree = view({ intents: [intent({ state: "approved", approval_rule: null })] });
    expect(badges(tree)).toEqual(["approved"]);
  });
});

describe("QueueView: a repeating story (#485)", () => {
  const WEEKLY = { unit: "week" as const, every: 1, days: [1, 4], at: "16:07" };
  const repeating = (over: Partial<Intent> = {}) =>
    intent({ origin: "planned", scheduled_by: "Ines", repeat: WEEKLY, ...over });

  it("says how it repeats beside who planned it", () => {
    expect(badges(view({ intents: [repeating()] }))).toContain(
      "Planned by Ines · Repeats every week on Mon and Thu at 4:07 PM",
    );
  });

  it("names the story's zone when its rule's time is not on the page's clock", () => {
    const tree = view({ intents: [repeating({ tz: "Europe/London" })], tz: "America/New_York" });
    expect(badges(tree)).toContain(
      "Planned by Ines · Repeats every week on Mon and Thu at 4:07 PM (Europe/London)",
    );
  });

  it("asks whether Cancel means this one or the series, and sends the answer", () => {
    const onAction = vi.fn();
    const row = repeating({ state: "scheduled" });
    const tree = view({ intents: [row], actionsOf: () => ["cancel"], onAction });

    const description = [...walk(tree)].find((el) => el.type === DialogDescription);
    expect(text(description)).toMatch(/^sample\.jpg repeats every week on Mon and Thu/);
    const choices = answers(tree);
    expect(choices.map((b) => text(b.props.children))).toEqual([
      "Skip this one",
      "Stop the series",
    ]);
    choices[0].props.onClick!();
    choices[1].props.onClick!();
    expect(onAction.mock.calls).toEqual([
      [row, "cancel"],
      [row, "cancel_series"],
    ]);
  });

  it("asks before Stop repeating, on a story already sent, and sends it as a tap", () => {
    const onAction = vi.fn();
    const row = repeating({ state: "awaiting_approval" });
    const tree = view({ intents: [row], actionsOf: () => ["stop_repeating"], onAction });

    const [trigger] = levers(tree);
    expect(text(trigger.props.children)).toBe("Stop repeating");
    const [confirm] = answers(tree);
    confirm.props.onClick!();
    expect(onAction).toHaveBeenCalledWith(row, "stop_repeating");
  });

  it("gives each row the anchor of its item on its account, which a calendar occurrence opens", () => {
    const [row] = [...walk(view({ intents: [repeating()] }))].filter((el) => el.type === "li");
    expect((row.props as { id: string }).id).toBe("story-media-1-account-1");
  });

  it("hands Edit series to the reschedule dialog, in its series form", () => {
    const onReschedule = vi.fn(async () => null);
    const row = repeating({ state: "scheduled" });
    const tree = view({ intents: [row], actionsOf: () => ["edit_series"], onReschedule });
    const dialog = [...walk(tree)].find((el) => el.type === RescheduleDialog) as ReactElement<
      Parameters<typeof RescheduleDialog>[0]
    >;
    expect(dialog.props.series).toBe(true);
    expect(dialog.props.label).toBe("Edit series…");
    expect(dialog.props.onSubmit).toBe(onReschedule);
  });
});

describe("QueueView: an item's link to add by hand (#1413)", () => {
  const links = (tree: ReactElement) =>
    [...walk(tree)]
      .filter((el) => el.type === ItemLink)
      .map((el) => (el.props as { link: string }).link);

  it("draws a story's item link on its row", () => {
    const linked = view({ intents: [intent({ link_url: "https://example.com/menu" })] });
    expect(links(linked)).toEqual(["https://example.com/menu"]);
  });

  it("draws nothing for a story whose item has no link", () => {
    expect(links(view())).toEqual([]);
  });
});

describe("a row's thumbnail (#1634)", () => {
  const WS = "11111111-1111-4111-8111-111111111111";
  type ThumbProps = Parameters<typeof MediaThumbnail>[0];

  const thumbnails = (tree: ReactElement) =>
    [...walk(tree)].filter(
      (el) => el.type === MediaThumbnail,
    ) as ReactElement<ThumbProps>[];
  const has = (tree: ReactElement, type: unknown) =>
    [...walk(tree)].some((el) => el.type === type);

  it("is fetched through this tier's route and named by the file", () => {
    const tree = view({
      workspaceId: WS,
      intents: [intent({ has_thumbnail: true, media_item_id: "media-9", thumbnail_version: "v1" })],
    });
    const [thumbnail] = thumbnails(tree);
    expect(thumbnail.props.src).toBe(`/api/workspaces/${WS}/media/media-9/thumbnail?v=v1`);
    expect(thumbnail.props.alt).toBe("sample.jpg");
    expect(thumbnail.props.video).toBe(false);
    // The glyph waits as the fallback, not beside the picture.
    expect((thumbnail.props.fallback as ReactElement).type).toBe(ImageIcon);
  });

  it("marks a video, and falls back to the video glyph", () => {
    const [thumbnail] = thumbnails(
      view({ workspaceId: WS, intents: [intent({ has_thumbnail: true, media_kind: "video" })] }),
    );
    expect(thumbnail.props.video).toBe(true);
    expect((thumbnail.props.fallback as ReactElement).type).toBe(Video);
  });

  it("draws the glyph, and asks for nothing, when the row has no thumbnail", () => {
    const tree = view({ workspaceId: WS, intents: [intent({ has_thumbnail: false })] });
    expect(thumbnails(tree)).toEqual([]);
    expect(has(tree, ImageIcon)).toBe(true);
  });

  it("draws the glyph without a workspace, which is how the sample workspace calls it", () => {
    const tree = view({ workspaceId: null, intents: [intent({ has_thumbnail: true })] });
    expect(thumbnails(tree)).toEqual([]);
    expect(has(tree, ImageIcon)).toBe(true);
  });
});

describe("an archive item's row (#1644)", () => {
  const WS = "11111111-1111-4111-8111-111111111111";
  const FAKE_ID = "17900000000000001";
  const GENERATION = "5b0e1c2e-8c67-4d29-9a3f-0c1d2e3f4a5b";
  type ThumbProps = Parameters<typeof MediaThumbnail>[0];

  /**
   * A story whose row carries the name `Instagram <id>`. The API stores
   * `Instagram post` and projects it on every read, so this is the guard's
   * input: the page names the item itself.
   */
  const archive = (over: Partial<Intent> = {}) =>
    intent({
      id: "row-ig",
      file_name: `Instagram ${FAKE_ID}`,
      media_item_id: "media-ig",
      has_thumbnail: true,
      thumbnail_version: `${GENERATION}.7`,
      provenance: {
        provider: "instagram_archive",
        origin_handle: "example_handle",
        original_at: "2020-01-01T12:00:00+00:00",
      },
      ...over,
    });
  const thumbnails = (tree: ReactElement) =>
    [...walk(tree)].filter((el) => el.type === MediaThumbnail) as ReactElement<ThumbProps>[];

  it("goes by its own name and says where it came from, never by the id it is stored under", () => {
    const tree = view({ workspaceId: WS, intents: [archive()], actionsOf: () => ["reject", "cancel"] });
    const words = text(tree);
    expect(words).toContain("Instagram post");
    expect(words).toContain("From @example_handle · Instagram, posted Jan 1, 2020");
    expect(JSON.stringify(words)).not.toContain(FAKE_ID);
    // The dialogs that name the item say the same.
    const bodies = [...walk(tree)]
      .filter((el) => el.type === DialogDescription)
      .map((el) => text(el));
    expect(bodies).toEqual([
      "Instagram post will never be offered again for example.brand. Skip instead if it should come back later.",
      "Instagram post won't post to example.brand. The item stays in your Media library.",
    ]);
  });

  it("asks for its picture at the route, under the archive's limits, in the row's box", () => {
    const [thumbnail] = thumbnails(view({ workspaceId: WS, intents: [archive()] }));
    expect(thumbnail.props.src).toBe(
      `/api/workspaces/${WS}/media/media-ig/thumbnail?v=${GENERATION}.7`,
    );
    expect(thumbnail.props.alt).toBe("Instagram post");
    expect(thumbnail.props.archive).toEqual({ workspaceId: WS, layout: "tile" });
  });

  it("takes the archive path for a row as the API serves it: already named, told by its provenance alone", () => {
    // The API names an archive item itself (`vocabulary.ARCHIVE_ITEM_NAME`) and sends no
    // Graph id, so what tells its picture from a Drive picture is the provenance on the row.
    const served = archive({ file_name: "Instagram post" });
    const tree = view({ workspaceId: WS, intents: [served], actionsOf: () => [] });
    const [thumbnail] = thumbnails(tree);
    expect(thumbnail.props.archive).toEqual({ workspaceId: WS, layout: "tile" });
    expect(thumbnail.props.alt).toBe("Instagram post");
    expect(text(tree)).toContain("From @example_handle · Instagram, posted Jan 1, 2020");
    // Without the provenance the same row is drawn as a Drive row: the browser's picture.
    const [plain] = thumbnails(
      view({ workspaceId: WS, intents: [{ ...served, provenance: null }], actionsOf: () => [] }),
    );
    expect(plain.props.archive).toBeUndefined();
  });

  it("prints a server slide name in the Queue", () => {
    const row = archive({
      file_name: "Instagram post, slide 3 of 10",
      provenance: {
        provider: "instagram_archive", origin_handle: "example_handle",
        original_at: "2020-01-01T12:00:00+00:00",
        slide: { post_ref: "synthetic-post", position: 3, count: 10 },
      },
    });
    const words = text(view({ intents: [row] }));
    expect(words).toContain("Instagram post, slide 3 of 10");
    expect(words).not.toContain("synthetic-post");
  });

  it("reads the day it was posted on the workspace's clock", () => {
    const row = archive({
      provenance: {
        provider: "instagram_archive",
        origin_handle: "example_handle",
        original_at: "2020-01-01T02:30:00+00:00",
      },
    });
    expect(text(view({ intents: [row], tz: "America/New_York" }))).toContain("posted Dec 31, 2019");
    expect(text(view({ intents: [row], tz: "UTC" }))).toContain("posted Jan 1, 2020");
  });

  it("hands a tap to onAction by the row's id, the name it was drawn under aside", () => {
    const onAction = vi.fn();
    const [approve] = levers(view({ intents: [archive()], actionsOf: () => ["approve"], onAction }));
    approve.props.onClick!();
    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onAction.mock.calls[0][0]).toMatchObject({ id: "row-ig", entered_state_at: archive().entered_state_at });
    expect(onAction.mock.calls[0][1]).toBe("approve");
  });

  it("leaves the Drive row beside it with its own name, no provenance line and the browser's picture", () => {
    const drive = intent({
      id: "row-drive",
      file_name: "Instagram 12.jpg",
      has_thumbnail: true,
      provenance: null,
    });
    const tree = view({ workspaceId: WS, intents: [archive(), drive], actionsOf: () => [] });
    const [, driveRow] = [...walk(tree)].filter((el) => el.type === "li");
    expect(text(driveRow)).toContain("Instagram 12.jpg");
    expect(text(driveRow)).not.toContain("From ");
    const [, driveThumb] = thumbnails(tree);
    expect(driveThumb.props.archive).toBeUndefined();
    expect(driveThumb.props.alt).toBe("Instagram 12.jpg");
  });
});
