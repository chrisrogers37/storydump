import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ADD_FOLDER_RESET_COPY,
  addDriveFolder,
  addFolderResetNote,
  addedFolderNotice,
  driveConnectControl,
  driveConnectWarning,
  driveStatusBadge,
  fetchDriveFolders,
  folderPickOutcome,
  folderRemoveOutcome,
  isGoogleAuthorizationUrl,
  removeDriveFolder,
  removeFolderRefusalCopy,
  removeFolderWarning,
  removedFolderNotice,
  requestDriveConnect,
  addFolderRefusalCopy,
  connectedFolderRefs,
  driveFoldersRefusalCopy,
  listedDriveSources,
  sourceFolderName,
  sourceStateLabel,
} from "./drive";
import { parseSourceMix, type SourceMix } from "./source-mix";

const WS = "11111111-1111-4111-8111-111111111111";
const SRC = "22222222-2222-4222-8222-222222222222";
const PAST_POSTS = "33333333-3333-4333-8333-333333333333";

let captured: { url: string; init?: RequestInit }[] = [];

function stubFetch(body: unknown, status = 200) {
  captured = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      captured.push({ url, init });
      return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("the one host the browser may be sent to", () => {
  it("is Google's account host over https, exactly", () => {
    expect(
      isGoogleAuthorizationUrl(
        "https://accounts.google.com/o/oauth2/v2/auth?x=1",
      ),
    ).toBe(true);
    expect(isGoogleAuthorizationUrl("http://accounts.google.com/")).toBe(false);
    expect(
      isGoogleAuthorizationUrl("https://accounts.google.com.evil.test/"),
    ).toBe(false);
    expect(
      isGoogleAuthorizationUrl("https://evil.test/accounts.google.com"),
    ).toBe(false);
  });
});

describe("the workspace's Drive grant, said on screen", () => {
  it("only active is ever green", () => {
    expect(driveStatusBadge("active").tone).toBe("active");
    for (const status of [
      "none",
      "expired",
      "revoked",
      "weird",
      null,
      undefined,
    ]) {
      expect(driveStatusBadge(status).tone, String(status)).not.toBe("active");
    }
  });
  it("expired and revoked share a remedy and keep distinct causes", () => {
    expect(driveStatusBadge("expired").label).toMatch(/expired/i);
    expect(driveStatusBadge("revoked").label).toMatch(/revoked/i);
    expect(driveStatusBadge("expired").tone).toBe(
      driveStatusBadge("revoked").tone,
    );
  });
  it("offers Connect before a grant and Reconnect after — including while live", () => {
    expect(driveConnectControl("none")).toEqual({
      label: "Connect Google Drive",
      kind: "connect",
    });
    expect(driveConnectControl("expired")?.kind).toBe("reconnect");
    expect(driveConnectControl("revoked")?.kind).toBe("reconnect");
    // Google can revoke on its side without the projection knowing; the road
    // back must not be Disconnect → Connect.
    expect(driveConnectControl("active")?.kind).toBe("reconnect");
  });
  it("warns about Google's unverified-app page wherever the grant is not live", () => {
    for (const status of [
      "none",
      "expired",
      "revoked",
      "weird",
      null,
      undefined,
    ]) {
      expect(driveConnectWarning(status), String(status)).not.toBeNull();
    }
    // A live grant's Reconnect is an optional repair, and its person has been
    // through Google's page once: the sentence is not shown on every
    // connected workspace.
    expect(driveConnectWarning("active")).toBeNull();
  });
  it("says what Google may show, the way past it, and what is asked for", () => {
    const warning = driveConnectWarning("none") ?? "";
    // "If": the page is Google's, worded and shown at its discretion.
    expect(warning).toMatch(/^If Google says it hasn't verified this app/);
    expect(warning).toMatch(/Advanced/);
    expect(warning).toContain('"Go to storydump (unsafe)"');
    expect(warning).toMatch(/read-only/);
  });
});

describe("the grant starts at its proxy", () => {
  it("posts to the workspace's drive connect route and hands back where to go", async () => {
    stubFetch({
      authorizationUrl: "https://accounts.google.com/o/oauth2/v2/auth?state=s",
    });
    const result = await requestDriveConnect(WS);
    expect(result.ok).toBe(true);
    expect(captured[0].url).toBe(`/api/workspaces/${WS}/drive/connect`);
    expect(captured[0].init?.method).toBe("POST");
  });
});

describe("the folder browser", () => {
  it("reads the root when no parent is given, and names the parent otherwise", async () => {
    stubFetch({ parent: "root", folders: [{ id: "f1", name: "Trips" }] });
    const root = await fetchDriveFolders(WS, null);
    expect(root).toEqual({
      ok: true,
      parent: "root",
      folders: [{ id: "f1", name: "Trips" }],
      truncated: false,
    });
    expect(captured[0].url).toBe(`/api/workspaces/${WS}/drive/folders`);

    stubFetch({ parent: "f1", folders: [] });
    await fetchDriveFolders(WS, "f1");
    expect(captured[0].url).toBe(
      `/api/workspaces/${WS}/drive/folders?parent=f1`,
    );
  });
  it("carries the API's refusal by name", async () => {
    stubFetch({ error: "drive_not_connected" }, 409);
    const result = await fetchDriveFolders(WS, null);
    expect(result).toEqual({
      ok: false,
      error: "drive_not_connected",
      status: 409,
    });
  });
  it("refuses a malformed listing rather than rendering it", async () => {
    stubFetch({ folders: "nope" });
    const result = await fetchDriveFolders(WS, null);
    expect(result.ok).toBe(false);
  });
});

/** The mix resource of a converted workspace, as the API answers it: Instagram past posts and one folder. */
function mixBody(over: Record<string, unknown> = {}) {
  return {
    protocol_version: 2,
    revision: 9,
    mode: "equal",
    supported_actions: ["reset_equal", "set_enabled", "save_custom"],
    conversion: { state: "complete", kind: "new_equal" },
    rows: [
      {
        source_id: PAST_POSTS,
        provider: "instagram_past_posts",
        name: "example_handle",
        state: "active",
        origin_handle: "example_handle",
        enabled: true,
        ratio: "0.5",
        target_share: "0.5",
        media_count: 700,
        indexed: 680,
      },
      {
        source_id: SRC,
        provider: "gdrive",
        name: "Trips",
        state: "active",
        enabled: true,
        ratio: "0.5",
        target_share: "0.5",
        media_count: 40,
      },
    ],
    destinations: [],
    past_posts_available: true,
    ...over,
  };
}

function mixOf(over: Record<string, unknown> = {}): SourceMix {
  const mix = parseSourceMix(mixBody(over));
  if (!mix) throw new Error("fixture did not parse");
  return mix;
}

/** The mix with the folder Off: what removing a folder that is off leaves to say. */
const FOLDER_OFF = {
  rows: [
    { ...mixBody().rows[0], ratio: "1", target_share: "1" },
    { ...mixBody().rows[1], enabled: false, ratio: "0", target_share: "0" },
  ],
};

const TRIPS: { id: string; name: string } = { id: "f1", name: "Trips" };

describe("picking and removing folders", () => {
  it("adds the picked folder by id AND name — the name is what the card shows", async () => {
    stubFetch({ sourceId: SRC, created: true }, 201);
    const result = await addDriveFolder(WS, { id: "f1", name: "Trips" });
    expect(result).toEqual({ ok: true, sourceId: SRC, created: true });
    expect(captured[0].url).toBe(`/api/workspaces/${WS}/sources`);
    expect(JSON.parse(String(captured[0].init?.body))).toEqual({
      folder_ref: "f1",
      folder_name: "Trips",
    });
  });
  it("removes a folder with a DELETE on the source", async () => {
    stubFetch({ source_id: SRC, state: "paused" });
    const result = await removeDriveFolder(WS, SRC);
    expect(result.ok).toBe(true);
    expect(captured[0].url).toBe(`/api/workspaces/${WS}/sources/${SRC}`);
    expect(captured[0].init?.method).toBe("DELETE");
  });
});

describe("a protocol-1 workspace is written to without protocol 2's fields", () => {
  // The API refuses protocol 2's fields on a workspace that has not converted,
  // so a pick or a removal that sent them there would fail every time.
  it("picks a folder with its two fields and nothing else, and reads no mix off the answer", async () => {
    // Even an answer that carries a mix is not read as one: it was not asked.
    stubFetch({ sourceId: SRC, created: true, mix: mixBody() }, 201);
    const result = await addDriveFolder(WS, TRIPS, null);
    expect(Object.keys(JSON.parse(String(captured[0].init?.body))).sort()).toEqual([
      "folder_name",
      "folder_ref",
    ]);
    expect(result).toEqual({ ok: true, sourceId: SRC, created: true });
    expect("mix" in result).toBe(false);
  });

  it("removes a folder with a bare DELETE: no body, no content type", async () => {
    stubFetch({ source_id: SRC, state: "paused", mix: mixBody() });
    const result = await removeDriveFolder(WS, SRC, null);
    expect(captured[0].init).toEqual({ method: "DELETE" });
    expect(result).toEqual({ ok: true });
  });
});

describe("a converted workspace (protocol 2) is written to against its revision", () => {
  it("picks a folder carrying the protocol and the revision it was made against, and hands back the mix answered", async () => {
    stubFetch({ sourceId: SRC, created: true, mix: mixBody({ revision: 9 }) }, 201);
    const result = await addDriveFolder(WS, TRIPS, 8);
    expect(captured[0].url).toBe(`/api/workspaces/${WS}/sources`);
    expect(captured[0].init?.method).toBe("POST");
    expect(JSON.parse(String(captured[0].init?.body))).toEqual({
      folder_ref: "f1",
      folder_name: "Trips",
      protocol_version: 2,
      expected_revision: 8,
    });
    if (!result.ok) throw new Error("expected the pick to land");
    expect(result.sourceId).toBe(SRC);
    expect(result.created).toBe(true);
    // The API's answer, parsed: the page says it as sent.
    expect(result.mix?.revision).toBe(9);
    expect(result.mix?.rows.map((row) => row.sourceId)).toEqual([PAST_POSTS, SRC]);
  });

  it("carries revision 0, which is a revision and not the absence of one", async () => {
    stubFetch({ sourceId: SRC, created: true, mix: mixBody({ revision: 1 }) }, 201);
    await addDriveFolder(WS, TRIPS, 0);
    expect(JSON.parse(String(captured[0].init?.body))).toMatchObject({
      protocol_version: 2,
      expected_revision: 0,
    });
    stubFetch({ source_id: SRC, state: "paused", mix: mixBody({ revision: 1 }) });
    await removeDriveFolder(WS, SRC, 0);
    expect(JSON.parse(String(captured[0].init?.body))).toEqual({
      protocol_version: 2,
      expected_revision: 0,
    });
  });

  it("still reports a pick that landed when the answer carries no readable mix", async () => {
    stubFetch({ sourceId: SRC, created: false, mix: "nope" }, 200);
    const result = await addDriveFolder(WS, TRIPS, 8);
    expect(result).toEqual({ ok: true, sourceId: SRC, created: false });
    // A mix that is protocol 1's is not a converted workspace's.
    stubFetch({ sourceId: SRC, created: true, mix: { protocol_version: 1, rows: [] } }, 201);
    expect(await addDriveFolder(WS, TRIPS, 8)).toEqual({ ok: true, sourceId: SRC, created: true });
  });

  it("removes a folder with a DELETE that carries the protocol and the revision, and hands back the mix answered", async () => {
    stubFetch({ source_id: SRC, state: "paused", mix: mixBody({ revision: 9, ...FOLDER_OFF }) });
    const result = await removeDriveFolder(WS, SRC, 8);
    expect(captured[0].url).toBe(`/api/workspaces/${WS}/sources/${SRC}`);
    expect(captured[0].init?.method).toBe("DELETE");
    expect(captured[0].init?.headers).toEqual({ "Content-Type": "application/json" });
    expect(JSON.parse(String(captured[0].init?.body))).toEqual({
      protocol_version: 2,
      expected_revision: 8,
    });
    if (!result.ok) throw new Error("expected the removal to land");
    expect(result.mix?.revision).toBe(9);
  });

  it("carries the API's refusal by name, a conflict's status included", async () => {
    stubFetch({ error: "mix_conflict", current_revision: 9 }, 409);
    expect(await addDriveFolder(WS, TRIPS, 8)).toEqual({
      ok: false,
      error: "mix_conflict",
      status: 409,
    });
    stubFetch({ error: "mix_protocol_required", required_protocol_version: 2 }, 400);
    expect(await removeDriveFolder(WS, SRC, null)).toEqual({
      ok: false,
      error: "mix_protocol_required",
      status: 400,
    });
  });
});

describe("a pick or a removal the mix has moved past, or that the page is too old for", () => {
  const FALLBACK_ADD = addFolderRefusalCopy("something_unforeseen");
  const FALLBACK_REMOVE = removeFolderRefusalCopy("something_unforeseen");

  it("has its own sentence for a conflict, which keeps the pick and says nothing was added", () => {
    const copy = addFolderRefusalCopy("mix_conflict");
    expect(copy).not.toBe(FALLBACK_ADD);
    expect(copy).toMatch(/posting mix changed/);
    expect(copy).toMatch(/nothing was added/);
    expect(copy).toMatch(/pick the folder again/);
    // It does not point at a mix on screen: the mix card is on General.
    expect(copy).not.toMatch(/is shown/);
  });

  it("has its own sentence for a page older than the mix, which says to reload", () => {
    const copy = addFolderRefusalCopy("mix_protocol_required");
    expect(copy).not.toBe(FALLBACK_ADD);
    expect(copy).toMatch(/older than the workspace's posting mix/);
    expect(copy).toMatch(/Reload the page/);
    expect(copy).toMatch(/nothing was added/);
  });

  it("says the same of a removal, and that nothing was removed", () => {
    const conflict = removeFolderRefusalCopy("mix_conflict");
    expect(conflict).not.toBe(FALLBACK_REMOVE);
    expect(conflict).toMatch(/posting mix changed/);
    expect(conflict).toMatch(/nothing was removed/);
    const stale = removeFolderRefusalCopy("mix_protocol_required");
    expect(stale).not.toBe(FALLBACK_REMOVE);
    expect(stale).toMatch(/Reload the page/);
    expect(stale).toMatch(/nothing was removed/);
  });

  it("stays on the pick and reads the mix again for those two refusals, and for no other", () => {
    for (const error of ["mix_conflict", "mix_protocol_required"]) {
      const pick = folderPickOutcome({ ok: false, error, status: 409 }, TRIPS);
      expect(pick).toEqual({ kind: "stay", error: addFolderRefusalCopy(error), reload: true });
      const remove = folderRemoveOutcome({ ok: false, error, status: 409 });
      expect(remove).toEqual({ kind: "stay", error: removeFolderRefusalCopy(error), reload: true });
    }
    // Positive control: every other refusal is said and the mix is left alone.
    for (const error of ["sources_changed", "drive_not_yours", "http_500"]) {
      expect(folderPickOutcome({ ok: false, error, status: 409 }, TRIPS)).toMatchObject({
        kind: "stay",
        reload: false,
      });
      expect(folderRemoveOutcome({ ok: false, error, status: 409 })).toMatchObject({
        kind: "stay",
        reload: false,
      });
    }
  });
});

describe("what a pick or a removal says it did to the posting mix", () => {
  it("says a pick and a removal on protocol 1 with no posting-mix line", () => {
    expect(addedFolderNotice("Trips", true)).toBe('"Trips" added. Its first sync starts shortly.');
    expect(addedFolderNotice("Trips", false)).toBe(
      '"Trips" was already a source here — it is syncing again.',
    );
    expect(removedFolderNotice()).toBe(
      "Folder removed from syncing. What was already synced stays; pick it again to resume.",
    );
    expect(folderPickOutcome({ ok: true, sourceId: SRC, created: true }, TRIPS)).toEqual({
      kind: "done",
      message: '"Trips" added. Its first sync starts shortly.',
    });
    expect(folderRemoveOutcome({ ok: true })).toEqual({
      kind: "done",
      message: removedFolderNotice(),
    });
  });

  it("adds the mix the API answered, as sent: its mode and each source that is on with its share", () => {
    const mix = mixOf();
    expect(addedFolderNotice("Trips", true, mix)).toBe(
      '"Trips" added. Its first sync starts shortly. Posting mix: Equal shares — Instagram past posts @example_handle 50%, Trips (Google Drive) 50%.',
    );
    expect(removedFolderNotice(mix)).toMatch(
      /pick it again to resume\. Posting mix: Equal shares — Instagram past posts @example_handle 50%, Trips \(Google Drive\) 50%\.$/,
    );
    expect(
      folderPickOutcome({ ok: true, sourceId: SRC, created: true, mix }, TRIPS),
    ).toEqual({ kind: "done", message: addedFolderNotice("Trips", true, mix) });
  });

  it("says Custom percentages when the answer is custom, and does not claim a reset it was not told of", () => {
    const custom = mixOf({
      mode: "custom",
      rows: [
        { ...mixBody().rows[0], ratio: "0.7", target_share: "0.7" },
        { ...mixBody().rows[1], ratio: "0.3", target_share: "0.3" },
      ],
    });
    const notice = removedFolderNotice(custom);
    expect(notice).toContain("Posting mix: Custom percentages — Instagram past posts @example_handle 70%, Trips (Google Drive) 30%.");
    expect(notice).not.toMatch(/reset|equal/i);
  });

  it("says when every source is Off, and shows an Off source as nothing", () => {
    const allOff = mixOf({
      mode: "custom",
      rows: [
        { ...mixBody().rows[0], enabled: false, ratio: "0", target_share: "0" },
        { ...mixBody().rows[1], enabled: false, ratio: "0", target_share: "0" },
      ],
    });
    expect(removedFolderNotice(allOff)).toMatch(/Posting mix: Custom percentages — every source is Off\.$/);
    const oneOff = mixOf(FOLDER_OFF);
    expect(removedFolderNotice(oneOff)).toMatch(/— Instagram past posts @example_handle 100%\.$/);
  });
});

describe("what is said before a folder is picked or removed", () => {
  it("tells the admin that adding a folder resets every share to equal, where the mix is converted", () => {
    expect(addFolderResetNote(mixOf())).toBe(ADD_FOLDER_RESET_COPY);
    expect(ADD_FOLDER_RESET_COPY).toMatch(/turns it on/);
    expect(ADD_FOLDER_RESET_COPY).toMatch(/resets every share to equal/);
    expect(ADD_FOLDER_RESET_COPY).toMatch(/custom percentages included/);
  });

  it("says nothing of shares on protocol 1, which has none to reset", () => {
    expect(addFolderResetNote(null)).toBeNull();
    expect(removeFolderWarning(null, SRC, "Trips")).toBeNull();
  });

  it("warns before a folder that is on is removed, naming it", () => {
    expect(removeFolderWarning(mixOf(), SRC, "Trips")).toBe(
      'Removing "Trips" resets every share to equal, custom percentages included.',
    );
  });

  it("does not ask before a folder that is Off is removed: it holds no share, and the API changes none", () => {
    expect(removeFolderWarning(mixOf(FOLDER_OFF), SRC, "Trips")).toBeNull();
    // The same mix, for the folder that is on: the Off case is the folder's own.
    expect(removeFolderWarning(mixOf(FOLDER_OFF), PAST_POSTS, "Instagram past posts")).not.toBeNull();
  });

  it("asks about a folder the mix does not list, as it would for one that is on", () => {
    expect(removeFolderWarning(mixOf(), "99999999-9999-4999-8999-999999999999", "Trips")).not.toBeNull();
  });
});

describe("connected folders and nested picks", () => {
  it("greys the folders that are sources here and not removed", () => {
    const refs = connectedFolderRefs([
      { folder_ref: "A", removed: false },
      { folder_ref: "B", removed: true },
      { folder_ref: null, removed: false },
      { folder_ref: "C", state: "active" },
      { folder_ref: "D", state: "paused" },
    ]);
    expect([...refs].sort()).toEqual(["A", "C"]);
  });
  it("lists the Drive folders still connected, in order: the Drive card's and the Approvals card's", () => {
    const listed = listedDriveSources([
      { id: "a", provider: "gdrive", state: "active" },
      { id: "b", provider: "gdrive", state: "paused", removed: false },
      { id: "c", provider: "gdrive", state: "error" },
      { id: "d", provider: "other", state: "active" },
      { id: "e", provider: "gdrive", state: "paused", removed: true },
      { id: "f", provider: "gdrive", state: "active", removed: true },
      { id: "g", provider: "gdrive", state: "paused" },
    ]);
    // Disconnect pauses a connected folder; only removal takes it off the cards.
    expect(listed.map((s) => s.id)).toEqual(["a", "b", "c", "g"]);
  });
  it("tells an admin who did not connect Drive who can browse it, at the browser and the pick", () => {
    const copy =
      "Only the person who connected Google Drive can browse it. Ask them to add the folder. Reconnecting Drive with your own account moves every connected folder onto your Drive, and folders you can't see there stop syncing.";
    expect(driveFoldersRefusalCopy("drive_not_yours")).toBe(copy);
    expect(addFolderRefusalCopy("drive_not_yours")).toBe(copy);
  });
  it("says why a nested folder pick was refused", () => {
    expect(addFolderRefusalCopy("source_nested")).toMatch(/already connected/);
    expect(addFolderRefusalCopy("sources_changed")).toMatch(/Try again/);
  });
});

describe("sourceStateLabel / sourceFolderName", () => {
  // One set of words for a folder, shared by the Drive card and the overview.
  it("names every source state in words, and prints a value outside the vocabulary as itself", () => {
    expect(sourceStateLabel("error")).toBe("Stopped syncing");
    expect(sourceStateLabel("paused")).toBe("Paused");
    expect(sourceStateLabel("active")).toBe("Active");
    expect(sourceStateLabel("resyncing")).toBe("resyncing");
    expect(sourceStateLabel(null)).toBe("Unknown state");
  });

  it("names a folder by the picker's name, else as a Drive folder", () => {
    expect(sourceFolderName({ folder_name: "Summer" })).toBe("Summer");
    expect(sourceFolderName({ folder_name: null })).toBe("Drive folder");
  });
});
