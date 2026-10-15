/**
 * What the web says of an archive item (#1644): where it came from by
 * its handle and the day, its own name whatever a row says (the API projects it
 * on every read, and the web guards a row that still carries `Instagram <id>`),
 * and a library of them listed 24 to a page. Every fixture is synthetic:
 * `example_handle` is no one's account and the id is no post's.
 */

import { readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { describe, expect, it } from "vitest";
import {
  ARCHIVE_ITEM_NAME,
  ARCHIVE_MEDIA_CHECKED_COPY,
  ARCHIVE_PAGE_SIZE,
  ARCHIVE_PROVIDER,
  PREVIEW_UNAVAILABLE_COPY,
  archiveProvenance,
  emptyLibrary,
  isArchiveMedia,
  pageOf,
  pastPostsOf,
  provenanceLabel,
  withArchiveName,
  type MediaProvenance,
} from "./archive-media";
import { MEDIA_CHECKED_COPY } from "./source-mix";

const FAKE_ID = "17900000000000001";

const provenance = (over: Partial<MediaProvenance> = {}): MediaProvenance => ({
  provider: ARCHIVE_PROVIDER,
  origin_handle: "example_handle",
  original_at: "2020-01-01T12:00:00+00:00",
  ...over,
});

describe("provenanceLabel", () => {
  it("says where a post came from and when, as the product words it", () => {
    expect(provenanceLabel(provenance())).toBe("From @example_handle · Instagram, posted Jan 1, 2020");
  });

  it("leaves out what the read does not have, and says no more than it knows", () => {
    expect(provenanceLabel(provenance({ original_at: null }))).toBe("From @example_handle · Instagram");
    expect(provenanceLabel(provenance({ origin_handle: null }))).toBe(
      "From Instagram, posted Jan 1, 2020",
    );
    expect(provenanceLabel(provenance({ origin_handle: null, original_at: null }))).toBe(
      "From Instagram",
    );
    // A blank handle is none, and so is a time that is not one: never "@" alone or "Invalid Date".
    expect(provenanceLabel(provenance({ origin_handle: "  ", original_at: "not a time" }))).toBe(
      "From Instagram",
    );
  });

  it("adds the @ once, however the API wrote the handle", () => {
    for (const handle of ["example_handle", "@example_handle", " @@example_handle "]) {
      expect(provenanceLabel(provenance({ origin_handle: handle, original_at: null }))).toBe(
        "From @example_handle · Instagram",
      );
    }
  });

  it("reads the day on the clock it is given, so a server and a browser say the same day", () => {
    const late = provenance({ original_at: "2020-01-01T02:30:00+00:00" });
    expect(provenanceLabel(late, "UTC")).toContain("posted Jan 1, 2020");
    expect(provenanceLabel(late, "America/New_York")).toContain("posted Dec 31, 2019");
    // No zone given is UTC, never the clock of whatever machine draws it.
    expect(provenanceLabel(late)).toBe(provenanceLabel(late, "UTC"));
    // A fractional second of any width, as Postgres writes it.
    expect(
      provenanceLabel(provenance({ original_at: "2020-06-15T08:00:00.123456+00:00" })),
    ).toContain("posted Jun 15, 2020");
  });

  it("carries no id, link or caption, only the handle and the day it was handed", () => {
    const label = provenanceLabel(provenance());
    expect(label).not.toMatch(/\d{10,}/);
    expect(label).not.toMatch(/https?:|instagram\.com|permalink|caption/i);
    // And a row that holds more than provenance does not reach it: the label reads three fields.
    const wide = { ...provenance(), media_id: FAKE_ID, permalink: "https://example.test/p/x" };
    expect(provenanceLabel(wide)).toBe(label);
  });
});

describe("archive rows are told from Drive rows by their provenance", () => {
  it("finds an archive row's provenance, and none on Drive, null or absent", () => {
    const row = { provenance: provenance() };
    expect(archiveProvenance(row)).toBe(row.provenance);
    expect(isArchiveMedia(row)).toBe(true);
    expect(archiveProvenance({ provenance: null })).toBeNull();
    expect(archiveProvenance({})).toBeNull();
    expect(isArchiveMedia({ provenance: null })).toBe(false);
    // Any other provider is not an archive row.
    expect(isArchiveMedia({ provenance: provenance({ provider: "gdrive" }) })).toBe(false);
  });
});

describe("withArchiveName", () => {
  const stored = `Instagram ${FAKE_ID}`;

  it("names an archive item for what it is, never by the id it is stored under", () => {
    const row = { id: "item-1", file_name: stored, provenance: provenance() };
    const drawn = withArchiveName(row);
    expect(drawn.file_name).toBe(ARCHIVE_ITEM_NAME);
    expect(ARCHIVE_ITEM_NAME).toBe("Instagram post");
    expect(JSON.stringify(drawn)).not.toContain(FAKE_ID);
    // The rest of the row is what the commands need, and it is untouched.
    expect(drawn).toEqual({ id: "item-1", file_name: "Instagram post", provenance: row.provenance });
    expect(row.file_name).toBe(stored);
  });

  it("returns every other row as it came, even one a person named Instagram", () => {
    const drive = { id: "item-2", file_name: stored, provenance: null };
    const bare = { id: "item-3", file_name: stored };
    expect(withArchiveName(drive)).toBe(drive);
    expect(withArchiveName(bare)).toBe(bare);
    expect(withArchiveName(drive).file_name).toBe(stored);
  });

  it("names a known slide and a truncated slide without exposing its post reference", () => {
    const slide = { post_ref: "synthetic-post", position: 3, count: 10 };
    const row = { file_name: stored, provenance: provenance({ slide }) };
    expect(withArchiveName(row).file_name).toBe("Instagram post, slide 3 of 10");
    expect(withArchiveName({ ...row, provenance: provenance({ slide: { ...slide, count: null } }) }).file_name)
      .toBe("Instagram post, slide 3");
    expect(withArchiveName({ file_name: "Instagram post, slide 3 of 10", provenance: provenance() }).file_name)
      .toBe("Instagram post, slide 3 of 10");
  });
});

describe("the words", () => {
  it("say an item's media is checked when used, and claim nothing it has not been", () => {
    expect(ARCHIVE_MEDIA_CHECKED_COPY).toBe("Media is checked when used.");
    // Not that it matches (the library lists items whatever a source's settings
    // leave out), and not that it was verified, is ready or was published.
    expect(ARCHIVE_MEDIA_CHECKED_COPY).not.toMatch(/match|verif|publish|ready|posted/i);
    expect(PREVIEW_UNAVAILABLE_COPY).toBe("Instagram preview unavailable");
  });

  it("are the Posting mix card's words for it, which this module cannot import", () => {
    expect(ARCHIVE_MEDIA_CHECKED_COPY).toBe(MEDIA_CHECKED_COPY);
  });
});

describe("pageOf", () => {
  const rows = Array.from({ length: 60 }, (_, i) => i);

  it("lists 24 to a page", () => {
    expect(ARCHIVE_PAGE_SIZE).toBe(24);
    const first = pageOf(rows, 0);
    expect(first.rows).toEqual(rows.slice(0, 24));
    expect(first.pages).toBe(3);
    expect(pageOf(rows, 1).rows).toEqual(rows.slice(24, 48));
    // The last page holds what is left.
    expect(pageOf(rows, 2).rows).toEqual(rows.slice(48));
  });

  it("holds a page to the pages there are, whatever it is asked for", () => {
    expect(pageOf(rows, 9).page).toBe(2);
    expect(pageOf(rows, -3).page).toBe(0);
    expect(pageOf(rows, Number.NaN).page).toBe(0);
    expect(pageOf(rows, 1.9).page).toBe(1);
  });

  it("is one empty page for nothing, and one page for exactly a page", () => {
    expect(pageOf([], 0)).toEqual({ rows: [], page: 0, pages: 1 });
    expect(pageOf(rows.slice(0, 24), 0).pages).toBe(1);
    expect(pageOf(rows.slice(0, 25), 0).pages).toBe(2);
  });
});

describe("pastPostsOf", () => {
  const row = (provider: string, enabled: boolean) => ({ provider, enabled });

  it("is on when a past-posts source is on, and Off when every one is Off", () => {
    expect(pastPostsOf({ rows: [row(ARCHIVE_PROVIDER, true)] })).toBe("on");
    expect(pastPostsOf({ rows: [row(ARCHIVE_PROVIDER, false)] })).toBe("off");
    // Several accounts, one source each: any that is on makes it on.
    expect(pastPostsOf({ rows: [row(ARCHIVE_PROVIDER, false), row(ARCHIVE_PROVIDER, true)] })).toBe("on");
    expect(pastPostsOf({ rows: [row(ARCHIVE_PROVIDER, false), row(ARCHIVE_PROVIDER, false)] })).toBe(
      "off",
    );
  });

  it("is judged by the past-posts sources alone, whatever Drive is", () => {
    expect(pastPostsOf({ rows: [row("gdrive", true), row(ARCHIVE_PROVIDER, false)] })).toBe("off");
    expect(pastPostsOf({ rows: [row("gdrive", false), row(ARCHIVE_PROVIDER, true)] })).toBe("on");
  });

  it("is nothing for a mix with no past-posts source, and for no mix", () => {
    expect(pastPostsOf({ rows: [row("gdrive", true)] })).toBeNull();
    expect(pastPostsOf({ rows: [] })).toBeNull();
    expect(pastPostsOf(null)).toBeNull();
  });
});

describe("emptyLibrary", () => {
  it("says to connect Google Drive, word for word, for a workspace with no past posts source", () => {
    // Pinned byte for byte: protocol 1, an unread mix and a mix with no past posts say this.
    expect(emptyLibrary(null)).toEqual({
      title: "No media items found",
      description: "Connect Google Drive and pick the folders to sync your content library.",
      action: { href: "/dashboard/settings?tab=integrations", label: "Set up Google Drive" },
    });
  });

  it("says past posts arrive as they are indexed, and that Drive is optional, when they are on", () => {
    const copy = emptyLibrary("on");
    expect(copy).toEqual({
      title: "No media items found",
      description:
        "Your past posts are on, so they appear here as they are indexed. Google Drive is optional.",
      action: { href: "/dashboard/settings?tab=general#posting-mix", label: "Open Posting mix" },
    });
  });

  it("says past posts are Off, and where to turn them on, and that Drive is optional, when they are Off", () => {
    expect(emptyLibrary("off")).toEqual({
      title: "No media items found",
      description:
        "Your past posts are Off, so none are indexed. Turn them on in Posting mix. Google Drive is optional.",
      action: { href: "/dashboard/settings?tab=general#posting-mix", label: "Open Posting mix" },
    });
  });

  it("never calls Drive a requirement to a workspace that has past posts, nor claims they are verified or ready", () => {
    for (const pastPosts of ["on", "off"] as const) {
      const { description, action } = emptyLibrary(pastPosts);
      expect(description).not.toMatch(/connect google drive|pick the folders|sync your content/i);
      expect(description).not.toMatch(/verif|ready|published/i);
      expect(action.label).not.toMatch(/drive/i);
      expect(action.href).not.toContain("integrations");
    }
  });
});

describe("the provider's spelling is the API's", () => {
  it("is the adapter's PROVIDER, so a rename cannot turn every archive item into a Drive item unseen", () => {
    // A drift would fail no request: every archive row would simply be drawn as
    // Drive, its pictures loaded without the archive's limits. AN UNREADABLE
    // SOURCE IS A FAILURE, NEVER A SKIP.
    const file = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../../../src/services/target/instagram_archive_adapter.py",
    );
    let source: string;
    try {
      source = readFileSync(file, "utf8");
    } catch (err) {
      throw new Error(
        `cannot read the archive adapter at ${file} — the contract is unverified, which is not the same as satisfied: ${err}`,
      );
    }
    expect(source.match(/^PROVIDER\s*=\s*"([^"]*)"\s*$/m)?.[1]).toBe(ARCHIVE_PROVIDER);
  });
});
