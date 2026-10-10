import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { SHARED_ROOT } from "@/lib/drive";
import type { DriveFolder } from "@/lib/drive";
import {
  DriveFolderPickerDialog,
  driveRootLabel,
  folderAlreadyConnected,
  pickerCurrentFolder,
  pickerParentRef,
  pickerStackAfter,
} from "./drive-folder-picker";

/**
 * The Drive folder picker's decisions, which had no test at all until #1216
 * — they were inline in an 821-line component, and `vitest.config.ts` pins
 * `environment: "node"` (it says why), so nothing that calls a hook can be
 * reached here. The picker answers that the way `api-tokens-tab.tsx` does:
 * the decisions are named exports the component calls at the point the logic
 * used to be written out, so this file and the screen cannot drift.
 *
 * What is NOT covered, stated rather than implied: no test opens the dialog
 * (the last block renders it closed, to a string, which shows its trigger and
 * nothing inside it), and none exercises `fetchDriveFolders`/`addDriveFolder`
 * from here — `lib/drive.test.ts` covers those doors.
 */

const A: DriveFolder = { id: "fid-a", name: "Campaigns" };
const B: DriveFolder = { id: "fid-b", name: "2026" };
const C: DriveFolder = { id: "fid-c", name: "Spring" };

describe("a folder that is already a source here", () => {
  it("is connected when its ref is in the set, and not otherwise", () => {
    const connected = new Set([A.id]);
    expect(folderAlreadyConnected(connected, A)).toBe(true);
    expect(folderAlreadyConnected(connected, B)).toBe(false);
  });

  it("offers everything when nothing is connected yet", () => {
    expect(folderAlreadyConnected(new Set<string>(), A)).toBe(false);
  });

  it("matches on the ref, never the name — two folders may share a name", () => {
    // Drive lets two folders in different parents carry the same name. Greying
    // the second because the first is connected would hide a pickable folder.
    const twin: DriveFolder = { id: "fid-twin", name: A.name };
    expect(folderAlreadyConnected(new Set([A.id]), twin)).toBe(false);
  });
});

describe("the breadcrumb a click leaves behind", () => {
  it("empties the stack for the root crumb, from any depth", () => {
    expect(pickerStackAfter([], -1)).toEqual([]);
    expect(pickerStackAfter([A, B, C], -1)).toEqual([]);
  });

  it("keeps everything up to and including the crumb clicked", () => {
    expect(pickerStackAfter([A, B, C], 0)).toEqual([A]);
    expect(pickerStackAfter([A, B, C], 1)).toEqual([A, B]);
    expect(pickerStackAfter([A, B, C], 2)).toEqual([A, B, C]);
  });

  it("cuts at the crumb clicked when a folder appears twice in the path", () => {
    // A shortcut can put the same folder at two depths. The cut is BY
    // POSITION: clicking the first crumb must not jump to the second.
    const stack = [A, B, A, C];
    expect(pickerStackAfter(stack, 0)).toEqual([A]);
    expect(pickerStackAfter(stack, 2)).toEqual([A, B, A]);
  });

  it("does not mutate the stack it was given", () => {
    const stack = [A, B, C];
    pickerStackAfter(stack, 0);
    expect(stack).toEqual([A, B, C]);
  });
});

describe("the folder the footer offers", () => {
  it("is nothing at a root listing — a root is not a folder anyone connects", () => {
    expect(pickerCurrentFolder([])).toBeNull();
  });

  it("is the one currently open", () => {
    expect(pickerCurrentFolder([A])).toBe(A);
    expect(pickerCurrentFolder([A, B, C])).toBe(C);
  });
});

describe("the parent each listing is read under", () => {
  it("is null at My Drive's root — the API reads the account's own root", () => {
    expect(pickerParentRef([], "mine")).toBeNull();
  });

  it("is the shared sentinel at the shared root, which is not a folder id", () => {
    expect(pickerParentRef([], "shared")).toBe(SHARED_ROOT);
  });

  it("is the open folder once inside one, whichever root it came from", () => {
    expect(pickerParentRef([A, B], "mine")).toBe(B.id);
    expect(pickerParentRef([A, B], "shared")).toBe(B.id);
  });
});

describe("the root crumb's label", () => {
  it("names the root the listing hangs from", () => {
    expect(driveRootLabel("mine")).toBe("My Drive");
    expect(driveRootLabel("shared")).toBe("Shared with me");
  });
});

/**
 * Add folder, which is where focus has to go back when the picker closes
 * (#1577). It shows what the button is. It does not show focus moving: that
 * takes a DOM.
 */
describe("the Add folder button", () => {
  const button = (disabled: boolean) =>
    renderToString(
      createElement(DriveFolderPickerDialog, {
        workspaceId: "ws-1",
        connectedRefs: new Set<string>(),
        disabled,
        onOpen: () => {},
        onPicked: () => {},
      }),
    ).match(/<button[^>]*>Add folder<\/button>/)?.[0] ?? "";

  it("opens the picker as its trigger, which is where Radix returns focus on close", () => {
    expect(button(false)).toContain('aria-haspopup="dialog"');
  });

  it("is dead while the workspace's grant is not active", () => {
    expect(button(false)).not.toMatch(/\sdisabled=""/);
    expect(button(true)).toMatch(/\sdisabled=""/);
  });
});
