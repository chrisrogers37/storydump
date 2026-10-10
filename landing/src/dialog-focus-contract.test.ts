/**
 * Every dialog has somewhere for focus to go back to (#1577).
 *
 * Radix returns focus to a dialog's trigger when the dialog closes, and to
 * nothing else: its modal content cancels the focus scope's own return and
 * focuses the trigger (`@radix-ui/react-dialog`, the content's
 * `onCloseAutoFocus`). A dialog opened from component state has no trigger.
 * However it closes, by Escape, the ×, Cancel or its own action, focus falls
 * to `<body>`, and a keyboard or screen-reader user is sent back to the top of
 * the page (WCAG 2.4.3, Focus Order).
 *
 * So every dialog and sheet in the tree opens through its own trigger, or says
 * where focus goes with `onCloseAutoFocus`. The Media Library's Schedule… does
 * the second: one dialog serves every row's button, so it has no one trigger.
 *
 * WHAT THIS CANNOT SEE. It matches text, and this suite has no DOM, so nothing
 * here watches focus move. It does not see:
 *  - a trigger that is gone or disabled by the time its dialog closes;
 *  - an `onCloseAutoFocus` that focuses nothing;
 *  - a trigger handed to a dialog from another file.
 *
 * AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP — the
 * `intent-states-contract` rule.
 */

import { readdirSync, readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { describe, expect, it } from "vitest";

const SRC = path.dirname(fileURLToPath(import.meta.url));

/** Each root built on Radix's Dialog, and the trigger Radix returns focus to. */
const TRIGGER_OF: Record<string, string> = {
  Dialog: "DialogTrigger",
  Sheet: "SheetTrigger",
};

/** Radix's overlays that return focus to a trigger. One wrapped in `components/ui` needs a row above. */
const RETURNS_TO_TRIGGER = /\b(?:AlertDialog|Dialog|DropdownMenu|Popover) as \w+[^}]*\}\s*from\s*["']radix-ui["']/;

function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return walk(full);
    return full.endsWith(".tsx") ? [full] : [];
  });
}

function sources(dir: string): { file: string; text: string }[] {
  return walk(dir).map((file) => {
    try {
      return { file: path.relative(SRC, file), text: readFileSync(file, "utf8") };
    } catch (err) {
      throw new Error(`cannot read ${file}: ${err}`);
    }
  });
}

/** Each `<Root …>…</Root>` in a source, as text. One that never closes cannot be read. */
function rootsOf(source: string, root: string): string[] {
  const open = new RegExp(`<${root}(?=[\\s>])`, "g");
  const bodies: string[] = [];
  for (let at = open.exec(source); at; at = open.exec(source)) {
    const end = source.indexOf(`</${root}>`, at.index);
    if (end < 0) throw new Error(`a <${root}> that never closes`);
    bodies.push(source.slice(at.index, end));
  }
  return bodies;
}

/** The roots in a source that give focus nowhere to go back to. */
function unanchored(source: string): string[] {
  return Object.entries(TRIGGER_OF).flatMap(([root, trigger]) =>
    rootsOf(source, root)
      .filter((body) => !body.includes(`<${trigger}`) && !body.includes("onCloseAutoFocus="))
      .map(() => root),
  );
}

describe("every dialog has somewhere for focus to go back to", () => {
  it("finds the tree's dialogs, so an empty search cannot pass as a clean one", () => {
    const withRoots = sources(SRC)
      .filter(({ text }) => Object.keys(TRIGGER_OF).some((root) => rootsOf(text, root).length > 0))
      .map(({ file }) => file);
    expect(withRoots).toEqual(
      expect.arrayContaining([
        "components/dashboard/nav-drawer.tsx",
        "components/dashboard/media/schedule-dialog.tsx",
        "components/dashboard/settings/danger-zone-card.tsx",
        "components/dashboard/settings/drive-folder-picker.tsx",
      ]),
    );
  });

  it("reads every primitive that returns focus to a trigger", () => {
    const overlays = sources(path.join(SRC, "components", "ui"))
      .filter(({ text }) => RETURNS_TO_TRIGGER.test(text))
      .map(({ file }) => path.basename(file));
    expect(overlays.sort()).toEqual(["dialog.tsx", "sheet.tsx"]);
  });

  it("holds across the tree", () => {
    const found = sources(SRC).flatMap(({ file, text }) =>
      unanchored(text).map((root) => `${file}: <${root}>`),
    );
    expect(found).toEqual([]);
  });

  it("reports a dialog that opens from state and says nothing about focus", () => {
    const opened = (inside: string, root = "Dialog") =>
      `<${root} open={open} onOpenChange={setOpen}>${inside}</${root}>`;
    expect(unanchored(opened("<DialogContent />"))).toEqual(["Dialog"]);
    expect(unanchored(opened("<SheetContent />", "Sheet"))).toEqual(["Sheet"]);
    expect(
      unanchored(opened("<DialogTrigger asChild><Button /></DialogTrigger><DialogContent />")),
    ).toEqual([]);
    expect(unanchored(opened("<DialogContent onCloseAutoFocus={back} />"))).toEqual([]);
    // A trigger beside the dialog is not its trigger: Radix reads it from inside the root.
    expect(unanchored(`<DialogTrigger />${opened("<DialogContent />")}`)).toEqual(["Dialog"]);
    expect(() => unanchored("<Dialog open={open} />")).toThrow();
  });
});
