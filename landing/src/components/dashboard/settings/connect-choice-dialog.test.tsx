import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Dialog } from "@/components/ui/dialog";
import {
  ADD_EXISTING_COPY,
  HISTORY_INDEXING_COPY,
  PUBLISHING_ONLY_COPY,
  RECONNECT_KEEPS_COPY,
  RESET_TO_EQUAL_COPY,
  parseConnectPreview,
  type ConnectPreview,
} from "@/lib/connect-choice";
import { TURN_ON_PAST_POSTS_COPY } from "@/lib/connection-result";
import {
  ConnectChoiceView,
  type ConnectChoiceHandlers,
  type ConnectChoiceState,
} from "./connect-choice-dialog";

/**
 * The past-posts dialog's body, drawn from each entry the connect preview
 * can answer (`environment: "node"`: markup, not a browser). The body takes
 * no hooks, so a state is a preview, the switch and a notice; it is drawn
 * inside a closed Radix root only so its title and description have their
 * dialog. Radix's own modal (focus trap, Escape, labelling) is not re-tested
 * here. Every handle is synthetic.
 */

const DRIVE = "33333333-3333-4333-8333-333333333333";
const ACCOUNT = "55555555-5555-4555-8555-555555555555";

function preview(over: Record<string, unknown> = {}): ConnectPreview {
  const parsed = parseConnectPreview({
    past_posts_available: true,
    entry: "add",
    account_id: null,
    handle: null,
    history: { editable: true, default: true, saved_enabled: null },
    expected_mix_revision: 8,
    preview_version: 1,
    reset: {
      applies: true,
      mode_after: "equal",
      rows_after: [
        { source_id: null, provider: "instagram_past_posts", name: null, target_share: "0.5" },
        { source_id: DRIVE, provider: "gdrive", name: "Event photos", target_share: "0.5" },
      ],
    },
    ...over,
  });
  if (!parsed) throw new Error("fixture did not parse");
  return parsed;
}

const RECONNECT = {
  entry: "reconnect",
  account_id: ACCOUNT,
  handle: "example_handle",
  history: { editable: false, default: null, saved_enabled: true },
  reset: null,
};

const on: ConnectChoiceHandlers = { toggle: () => {}, confirm: () => {}, cancel: () => {} };

function draw(state: Partial<ConnectChoiceState> & { preview: ConnectPreview }): string {
  return renderToStaticMarkup(
    createElement(
      Dialog,
      { open: false },
      createElement(ConnectChoiceView, {
        state: {
          useHistory: state.preview.defaultOn,
          notice: null,
          error: null,
          busy: false,
          ...state,
        },
        ids: "ID",
        on,
      }),
    ),
  );
}

/** The markup's text, tags dropped and entities read back, spaces collapsed. */
function text(html: string): string {
  return html
    .replace(/<[^>]+>/g, " ")
    .replace(/&#x27;/g, "'")
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ")
    .trim();
}

describe("the header's Add", () => {
  const html = draw({ preview: preview() });

  it("offers the switch on by default, labelled and described", () => {
    expect(text(html)).toContain("Connect Instagram");
    expect(html).toMatch(/<label[^>]*for="ID-history"[^>]*>Use my past posts as a content source<\/label>/);
    const control = html.match(/<button[^>]*role="switch"[^>]*>/)?.[0] ?? "";
    expect(control).toContain('id="ID-history"');
    expect(control).toContain('aria-checked="true"');
    expect(control).toContain('aria-describedby="ID-history-detail"');
    expect(html).toContain(`id="ID-history-detail"`);
    expect(text(html)).toContain(HISTORY_INDEXING_COPY);
  });

  it("says when media is downloaded: a post that is picked or previewed", () => {
    // Fetch fresh bytes only for a selected post or an explicit preview.
    expect(HISTORY_INDEXING_COPY).toBe(
      "We index your past posts' details — no media is downloaded until a post is picked or previewed. Media is checked when used.",
    );
    expect(text(html)).toContain("until a post is picked or previewed");
  });

  it("says an existing account keeps its settings", () => {
    expect(text(html)).toContain(ADD_EXISTING_COPY);
  });

  it("shows the equal shares an On choice would leave, and that custom shares reset", () => {
    expect(text(html)).toContain(RESET_TO_EQUAL_COPY);
    expect(text(html)).toContain("This account's past posts (new) 50%");
    expect(text(html)).toContain("Event photos · Google Drive 50%");
    expect(text(html)).not.toContain(PUBLISHING_ONLY_COPY);
  });

  it("with the switch off, connects for publishing only and shows no reset", () => {
    const off = draw({ preview: preview(), useHistory: false });
    expect(off).toContain('aria-checked="false"');
    expect(text(off)).toContain(PUBLISHING_ONLY_COPY);
    expect(text(off)).not.toContain(RESET_TO_EQUAL_COPY);
  });

  it("keeps a source that is Off as Off in the preview", () => {
    const withOff = preview({
      reset: {
        applies: true,
        mode_after: "equal",
        rows_after: [
          { source_id: null, provider: "instagram_past_posts", name: null, target_share: "1" },
          { source_id: DRIVE, provider: "gdrive", name: "Event photos", target_share: "0" },
        ],
      },
    });
    expect(text(draw({ preview: withOff }))).toContain("Event photos · Google Drive Off");
  });

  it("shows no reset when the server says nothing would reset", () => {
    const none = preview({ reset: null });
    expect(text(draw({ preview: none }))).not.toContain(RESET_TO_EQUAL_COPY);
  });
});

describe("a handle-only row's first connect", () => {
  const first = preview({
    entry: "first_connect",
    account_id: ACCOUNT,
    handle: "example_handle",
    reset: {
      applies: true,
      mode_after: "equal",
      rows_after: [
        { source_id: null, provider: "instagram_past_posts", name: "example_handle", target_share: "1" },
      ],
    },
  });
  const html = draw({ preview: first });

  it("names the handle in the title, the switch and the preview", () => {
    expect(text(html)).toContain("Connect @example_handle");
    expect(html).toContain(">Use @example_handle&#x27;s past posts as a content source</label>");
    expect(text(html)).toContain("@example_handle's past posts (new) 100%");
  });

  it("does not speak of an existing account, which this is not", () => {
    expect(text(html)).not.toContain(ADD_EXISTING_COPY);
  });
});

describe("a reconnect", () => {
  it("shows the saved setting read-only, with no switch and no reset", () => {
    const html = draw({ preview: preview(RECONNECT) });
    expect(text(html)).toContain("Reconnect @example_handle");
    expect(text(html)).toContain(`Past posts: On ${RECONNECT_KEEPS_COPY}`);
    expect(html).not.toContain('role="switch"');
    expect(text(html)).not.toContain(RESET_TO_EQUAL_COPY);
    expect(text(html)).not.toContain(PUBLISHING_ONLY_COPY);
    // Past posts are already on: there is nothing to turn on.
    expect(text(html)).not.toContain(TURN_ON_PAST_POSTS_COPY);
  });

  it("says Off when Off is what it keeps, and where to turn past posts on", () => {
    const off = preview({ ...RECONNECT, history: { editable: false, default: null, saved_enabled: false } });
    const words = text(draw({ preview: off }));
    expect(words).toContain(`Past posts: Off ${RECONNECT_KEEPS_COPY} Turn past posts on in Posting mix.`);
    // Reconnecting is never enablement, and there is still no switch to press here.
    expect(draw({ preview: off })).not.toContain('role="switch"');
  });
});

describe("what the dialog says while it works", () => {
  it("announces why it asks again politely, and a refusal assertively", () => {
    const html = draw({ preview: preview(), notice: "The posting mix changed.", error: "Refused." });
    expect(html).toMatch(/role="status"[^>]*>The posting mix changed\./);
    expect(html).toMatch(/role="alert"[^>]*>Refused\./);
  });

  it("holds every control while a start is in flight", () => {
    const html = draw({ preview: preview(), busy: true });
    expect(text(html)).toContain("Opening Instagram...");
    // `disabled=""` is the attribute; the bare word is also in every button's classes.
    expect(html.match(/<button[^>]*role="switch"[^>]*>/)?.[0]).toContain('disabled=""');
    for (const label of ["Cancel", "Opening Instagram..."]) {
      expect(html).toMatch(new RegExp(`<button[^>]*disabled=""[^>]*>${label.replace(/\./g, "\\.")}</button>`));
    }
    const idle = draw({ preview: preview() });
    expect(idle).toMatch(/<button[^>]*>Continue to Instagram<\/button>/);
    expect(idle).not.toContain('disabled=""');
  });
});
