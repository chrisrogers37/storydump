/**
 * The Settings page's role gates, each read off the page's own tree:
 *
 *  - The Members card's invite section is an admin's (#1600). A member is
 *    shown neither the invite form nor the pending list, and the page never
 *    reads the invitations for them: the listing carries the invitees'
 *    addresses (#1571).
 *  - The Approvals card (#1477) reaches Settings › General with what the page
 *    read, and is editable by an admin or the owner only (positions are an
 *    admin's), like the mix card beside it.
 *
 * The page, an async server component, is called directly with its doors
 * mocked (the session guard, the workspace and target fetches), and the
 * returned element tree is read without rendering it, as in
 * `overview-conditions.test.tsx`.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactElement, type ReactNode } from "react";

const { workspaceFetch, viewer } = vi.hoisted(() => ({
  workspaceFetch: vi.fn(),
  viewer: { role: "member" },
}));

vi.mock("@/lib/page-guards", () => ({
  requireWorkspacePage: async () => ({
    workspaceId: "ws-1",
    session: {
      userId: "u-1",
      workspaces: [{ id: "ws-1", name: "WS", role: viewer.role, state: "active" }],
    },
  }),
}));
vi.mock("@/lib/workspaces", () => ({ workspaceFetch }));
vi.mock("@/lib/target-api", () => ({ targetFetch: async () => ({ ok: true, data: { tokens: [] } }) }));
vi.mock("@/lib/session", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/session")>()),
  getSessionToken: async () => "tok",
}));

import SettingsPage from "./page";
import { ApprovalsCard } from "@/components/dashboard/settings/approvals-card";
import { MembersCard } from "@/components/dashboard/settings/members-card";
import { InviteMember } from "@/components/dashboard/settings/invite-member";

/**
 * Every element in a returned tree. Unlike a children-only walk, it also enters
 * element-valued props: the Members and Approvals cards reach the page's tree
 * as `GeneralTab`'s `members` and `approvals` props, not as children.
 */
function* walk(node: ReactNode): Generator<ReactElement> {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  if (!isValidElement(node)) return;
  yield node;
  for (const value of Object.values(node.props as Record<string, unknown>)) {
    if (isValidElement(value) || Array.isArray(value)) yield* walk(value as ReactNode);
  }
}

const ok = (data: unknown) => ({ ok: true, data });

const FOLDER = {
  id: "src-1",
  provider: "gdrive",
  state: "active",
  next_sync_at: null,
  last_sync_success_at: null,
  alerted_at: null,
  created_at: "2026-10-01T13:00:00+00:00",
  folder_ref: "f1",
  folder_name: "Tips",
  removed: false,
  approval_mode: "manual",
};

const INVITATION = {
  id: "inv-1",
  delivery_channel: "email",
  email: "partner@example.com",
  role: "member",
  state: "pending",
  expires_at: "2026-10-12T20:00:00+00:00",
  invited_by_user_id: "u-1",
  created_at: "2026-10-05T20:00:00+00:00",
};

/** Answer each read by its path. Every read the page bails on is ok. */
function answer(path: string) {
  switch (path) {
    case "":
      return ok({
        id: "ws-1",
        name: "WS",
        state: "active",
        tz: "UTC",
        approval_mode: "returning",
        api_publishing_enabled: true,
      });
    case "accounts":
      return ok({ accounts: [] });
    case "sources":
      return ok({ sources: [FOLDER] });
    case "bindings":
      return ok({ bindings: [] });
    case "members":
      return ok({ members: [] });
    case "stats":
      return ok({ intents_by_state: {}, media_by_state: {}, posts_by_day: [], accounts: 0, sources: 1 });
    case "drive":
      return ok({ drive: null });
    case "category-mix":
      return ok({ rows: [] });
    case "tokens":
      return ok({ tokens: [] });
    case "invitations":
      return ok({ invitations: [INVITATION] });
  }
  return { ok: false, status: 404, error: "http_404" };
}

async function renderAs(role: string) {
  viewer.role = role;
  const tree = await SettingsPage({ searchParams: Promise.resolve({}) });
  const elements = [...walk(tree)];
  return {
    cards: elements.filter((el) => el.type === MembersCard),
    invites: elements.filter((el) => el.type === InviteMember),
    approvals: elements.filter((el) => el.type === ApprovalsCard) as ReactElement<
      Parameters<typeof ApprovalsCard>[0]
    >[],
    readInvitations: workspaceFetch.mock.calls.some(([path]) => path === "invitations"),
  };
}

beforeEach(() => {
  workspaceFetch.mockReset();
  workspaceFetch.mockImplementation(async (path: string) => answer(path));
});

describe("the Members card's invite section, by the viewer's role", () => {
  it("shows a member the card but neither the invite form nor the pending list, and reads no invitations", async () => {
    const seen = await renderAs("member");
    // The walk reaches the card, so the missing section is not a walk that missed it.
    expect(seen.cards).toHaveLength(1);
    expect(seen.invites).toEqual([]);
    expect(seen.readInvitations).toBe(false);
  });

  for (const role of ["admin", "owner"]) {
    it(`gives ${role === "admin" ? "an admin" : "the owner"} the section, with the pending list it read`, async () => {
      const seen = await renderAs(role);
      expect(seen.cards).toHaveLength(1);
      expect(seen.invites).toHaveLength(1);
      expect(seen.readInvitations).toBe(true);
      const { invitations } = seen.invites[0].props as { invitations: { email: string }[] };
      expect(invitations.map((i) => i.email)).toEqual(["partner@example.com"]);
    });
  }
});

describe("the Approvals card on Settings › General", () => {
  async function approvalsCardAs(role: string) {
    const { approvals } = await renderAs(role);
    expect(approvals, "the page renders one Approvals card").toHaveLength(1);
    return approvals[0];
  }

  it("is handed the workspace's position, its folders and its posting setting", async () => {
    const { props } = await approvalsCardAs("admin");
    expect(props.workspaceId).toBe("ws-1");
    expect(props.position).toBe("returning");
    expect(props.sources.map((s) => [s.id, s.approval_mode])).toEqual([["src-1", "manual"]]);
    expect(props.apiPublishing).toBe(true);
  });

  it("is not keyed on the positions, so a re-read keeps it mounted and the focus in it", async () => {
    expect((await approvalsCardAs("admin")).key).toBeNull();
  });

  it("is editable by an admin and the owner, and held for a member", async () => {
    expect((await approvalsCardAs("admin")).props.editable).toBe(true);
    expect((await approvalsCardAs("owner")).props.editable).toBe(true);
    expect((await approvalsCardAs("member")).props.editable).toBe(false);
  });
});
