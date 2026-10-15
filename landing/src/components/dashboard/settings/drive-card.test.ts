import { beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactElement } from "react";
import type { SourceRow } from "@/lib/dashboard-payloads";
import { Button } from "@/components/ui/button";
import { DriveCard } from "./drive-card";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("react", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react")>()),
  useState: (initial: unknown) => [initial, vi.fn()],
  useId: () => "drive-hint",
}));

function* walk(node: unknown): Generator<ReactElement> {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
  } else if (isValidElement(node)) {
    yield node;
    yield* walk((node.props as { children?: unknown }).children);
  }
}
function text(node: unknown): string {
  if (typeof node === "string") return node;
  if (Array.isArray(node)) return node.map(text).join("");
  if (isValidElement(node)) return text((node.props as { children?: unknown }).children);
  return "";
}
const source: SourceRow = {
  id: "22222222-2222-4222-8222-222222222222", provider: "gdrive",
  state: "paused", approval_mode: "auto", removed: false,
  folder_name: "Tips", folder_ref: "folder-1", next_sync_at: null,
  last_sync_success_at: null, alerted_at: null, created_at: "2026-10-01T13:00:00Z",
};

beforeEach(() => vi.clearAllMocks());
describe("Drive folders after disconnect", () => {
  it("shows the paused folder and its badge while keeping sync held on a dead grant", () => {
    const tree = DriveCard({
      workspaceId: "11111111-1111-4111-8111-111111111111",
      sources: [source, { ...source, id: "removed", folder_name: "Removed folder", state: "active", removed: true }],
      drive: { status: "revoked", connected_at: null },
      mix: null,
      onError: vi.fn(), onNotice: vi.fn(),
    });
    expect(text(tree)).toContain("Tips");
    expect(text(tree)).toContain("Paused");
    expect(text(tree)).not.toContain("Removed folder");
    const sync = [...walk(tree)].find((el) => el.type === Button && text(el) === "Sync Now");
    expect((sync!.props as { disabled: boolean }).disabled).toBe(true);
  });
});
