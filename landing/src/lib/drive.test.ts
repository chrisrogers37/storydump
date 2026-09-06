import { afterEach, describe, expect, it, vi } from "vitest";
import {
  addDriveFolder,
  driveConnectControl,
  driveStatusBadge,
  fetchDriveFolders,
  isGoogleAuthorizationUrl,
  removeDriveFolder,
  requestDriveConnect,
} from "./drive";

const WS = "11111111-1111-4111-8111-111111111111";
const SRC = "22222222-2222-4222-8222-222222222222";

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
