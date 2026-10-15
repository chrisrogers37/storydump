"use client";

import { ChevronRight } from "lucide-react";
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  addDriveFolder,
  addFolderResetNote,
  driveFoldersRefusalCopy,
  fetchDriveFolders,
  folderPickOutcome,
  SHARED_ROOT,
} from "@/lib/drive";
import type { DriveFolder } from "@/lib/drive";
import type { SourceMix } from "@/lib/source-mix";

/**
 * The Drive folder browser: its trigger, its dialog, and the eight pieces of
 * state that are nobody else's business.
 *
 * A component of its own (#1216), so a keystroke in the picker re-renders
 * nothing beside it, and the picker's decisions can be reached by a test.
 *
 * ── Why the pure parts are exported separately ───────────────────────────
 *
 * `vitest.config.ts` pins `environment: "node"` and says why, so no test in
 * this app presses a button. `api-tokens-tab.tsx` answers that by exporting
 * its decisions beside its components (`mintFormValid`, `tokenStateBadge`,
 * `secretSlot`), and `api-tokens-tab.test.ts` reaches those. The five
 * functions below are this file's equivalent: the component calls each one
 * exactly where the logic runs, so the test and the screen
 * cannot drift apart.
 */

/** My Drive, or the shared-with-me pseudo-root. */
export type DriveRoot = "mine" | "shared";

/** The first breadcrumb — the root the listing is hanging from. */
export function driveRootLabel(root: DriveRoot): string {
  return root === "shared" ? "Shared with me" : "My Drive";
}

/**
 * The parent one listing is read under: the folder on top of the stack, or
 * the root itself when the stack is empty. "Shared with me" is a SENTINEL
 * (`SHARED_ROOT`), not a folder id, and My Drive is `null` — the API reads
 * the account's own root when no parent is named.
 */
export function pickerParentRef(
  stack: DriveFolder[],
  root: DriveRoot,
): string | null {
  return stack.length > 0
    ? stack[stack.length - 1].id
    : root === "shared"
      ? SHARED_ROOT
      : null;
}

/**
 * The stack a breadcrumb click leaves behind. `-1` is the root crumb, which
 * empties it; crumb `i` keeps everything up to and including `i`. The cut is
 * BY POSITION, never by id, so a folder that appears twice in one path is
 * still cut at the crumb that was actually clicked.
 */
export function pickerStackAfter(
  stack: DriveFolder[],
  index: number,
): DriveFolder[] {
  return index < 0 ? [] : stack.slice(0, index + 1);
}

/**
 * The folder the footer's "Use this one" targets: whichever is open. A root
 * listing has none — a root is not a folder anyone connects — so the footer
 * button is absent rather than disabled.
 */
export function pickerCurrentFolder(stack: DriveFolder[]): DriveFolder | null {
  return stack.length > 0 ? stack[stack.length - 1] : null;
}

/**
 * Folders that are sources here already (active ones): greyed in the picker
 * — a re-pick is a no-op and a pick inside one is refused by the API
 * (`source_nested`: connected folders are disjoint).
 */
export function folderAlreadyConnected(
  connectedRefs: Set<string>,
  folder: DriveFolder,
): boolean {
  return connectedRefs.has(folder.id);
}

export function DriveFolderPickerDialog({
  workspaceId,
  connectedRefs,
  disabled,
  mix,
  onOpen,
  onPicked,
  onStale,
}: {
  workspaceId: string;
  /** The folder refs already connected here — greyed rather than offered. */
  connectedRefs: Set<string>;
  /** The trigger is dead while the workspace's grant is not active. */
  disabled: boolean;
  /**
   * The workspace's posting mix as the page read it, when it is converted
   * (protocol 2); null on protocol 1. A pick is made against its revision, and
   * the dialog says, before the pick, that it resets the shares.
   */
  mix: SourceMix | null;
  /** Opening clears the tab's banners, as every other action on it does. */
  onOpen: () => void;
  /** A pick is the tab's news to report and its reason to re-read. */
  onPicked: (message: string) => void;
  /** The pick was refused because the page's mix is out of date: read it again, and keep the dialog. */
  onStale: () => void;
}) {
  const resetId = useId();
  const resetNote = addFolderResetNote(mix);
  const [open, setOpen] = useState(false);
  const [stack, setStack] = useState<DriveFolder[]>([]);
  const [folders, setFolders] = useState<DriveFolder[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pickingId, setPickingId] = useState<string | null>(null);
  const [root, setRoot] = useState<DriveRoot>("mine");
  const [truncated, setTruncated] = useState(false);

  const current = pickerCurrentFolder(stack);

  /** The folder browser: one listing per level, read through the grant. */
  async function loadFolders(nextStack: DriveFolder[], nextRoot: DriveRoot) {
    setLoading(true);
    setError(null);
    setFolders(null);
    setTruncated(false);
    const parent = pickerParentRef(nextStack, nextRoot);
    const result = await fetchDriveFolders(workspaceId, parent);
    setLoading(false);
    if (!result.ok) {
      setError(driveFoldersRefusalCopy(result.error));
      return;
    }
    setFolders(result.folders);
    setTruncated(result.truncated);
  }

  function openPicker() {
    onOpen();
    setOpen(true);
    setStack([]);
    setRoot("mine");
    void loadFolders([], "mine");
  }

  function closePicker() {
    setOpen(false);
    setFolders(null);
    setError(null);
    setStack([]);
    setTruncated(false);
  }

  function switchRoot(nextRoot: DriveRoot) {
    setRoot(nextRoot);
    setStack([]);
    void loadFolders([], nextRoot);
  }

  function enterFolder(folder: DriveFolder) {
    const next = [...stack, folder];
    setStack(next);
    void loadFolders(next, root);
  }

  function goTo(index: number) {
    const next = pickerStackAfter(stack, index);
    setStack(next);
    void loadFolders(next, root);
  }

  /**
   * Pick a folder: the source is created (or revived, if it had been removed)
   * and armed for its first sync. `created` matters to a person: the same
   * folder picked twice is the SAME source, and saying "added" both times
   * would hide that.
   *
   * A refusal leaves the dialog where it was, on the same listing: a pick the
   * mix had moved past (`mix_conflict`) is answered by reading the mix again,
   * and the folder is one press from being picked on the new revision.
   */
  async function pickFolder(folder: DriveFolder) {
    setError(null);
    setPickingId(folder.id);
    const result = await addDriveFolder(workspaceId, folder, mix?.revision ?? null);
    setPickingId(null);
    const outcome = folderPickOutcome(result, folder);
    if (outcome.kind === "stay") {
      setError(outcome.error);
      if (outcome.reload) onStale();
      return;
    }
    closePicker();
    onPicked(outcome.message);
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => (next ? openPicker() : closePicker())}
    >
      <DialogTrigger asChild>
        <Button size="sm" variant="outline" disabled={disabled}>
          Add folder
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Pick a Drive folder</DialogTitle>
          <DialogDescription>
            Each folder you connect is a group of its own: everything inside
            it syncs, at any depth. Open a folder to pick one of its
            subfolders — to weight two subfolders separately, connect each as
            its own folder.
          </DialogDescription>
        </DialogHeader>
        {resetNote && (
          <p id={resetId} className="text-sm text-muted-foreground">
            {resetNote}
          </p>
        )}
        <div className="flex gap-2">
          <Button
            size="sm"
            variant={root === "mine" ? "default" : "outline"}
            onClick={() => switchRoot("mine")}
            disabled={loading}
          >
            My Drive
          </Button>
          <Button
            size="sm"
            variant={root === "shared" ? "default" : "outline"}
            onClick={() => switchRoot("shared")}
            disabled={loading}
          >
            Shared with me
          </Button>
        </div>
        <div className="flex flex-wrap items-center gap-1 text-sm">
          <button
            type="button"
            className="underline-offset-2 hover:underline"
            onClick={() => goTo(-1)}
          >
            {driveRootLabel(root)}
          </button>
          {stack.map((f, i) => (
            <span key={f.id} className="flex items-center gap-1">
              <span className="text-muted-foreground">›</span>
              <button
                type="button"
                className="underline-offset-2 hover:underline"
                onClick={() => goTo(i)}
              >
                {f.name}
              </button>
            </span>
          ))}
        </div>
        {error && <p className="text-sm text-red-700">{error}</p>}
        {truncated && (
          <p className="text-xs text-muted-foreground">
            Showing the first folders alphabetically — this level has more.
            Open a folder to narrow the list.
          </p>
        )}
        <div className="max-h-72 overflow-y-auto rounded-md border">
          {loading ? (
            <p className="p-3 text-sm text-muted-foreground">
              Loading folders...
            </p>
          ) : folders !== null && folders.length === 0 ? (
            <p className="p-3 text-sm text-muted-foreground">
              No folders inside this one.
            </p>
          ) : (
            (folders ?? []).map((f) => (
              <div
                key={f.id}
                className="flex items-center justify-between gap-2 border-b px-3 py-2 last:border-b-0"
              >
                <button
                  type="button"
                  className="flex min-w-0 flex-1 items-center gap-1 text-left text-sm hover:underline"
                  onClick={() => enterFolder(f)}
                  aria-label={`Open ${f.name}`}
                  title="Open this folder"
                >
                  <span className="truncate">{f.name}</span>
                  <ChevronRight
                    className="size-4 shrink-0 text-muted-foreground"
                    aria-hidden
                  />
                </button>
                {folderAlreadyConnected(connectedRefs, f) ? (
                  <span className="text-xs text-muted-foreground">
                    Already connected
                  </span>
                ) : (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => pickFolder(f)}
                    disabled={pickingId !== null}
                    aria-describedby={resetNote ? resetId : undefined}
                  >
                    {pickingId === f.id ? "Adding..." : "Use this folder"}
                  </Button>
                )}
              </div>
            ))
          )}
        </div>
        <DialogFooter>
          {current && (
            <Button
              onClick={() => pickFolder(current)}
              disabled={pickingId !== null}
              aria-describedby={resetNote ? resetId : undefined}
            >
              {pickingId === current.id
                ? "Adding..."
                : `Use "${current.name}"`}
            </Button>
          )}
          <Button variant="ghost" onClick={closePicker}>
            Cancel
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
