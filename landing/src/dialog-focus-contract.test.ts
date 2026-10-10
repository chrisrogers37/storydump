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
 * So every dialog and sheet in the tree opens through its own trigger. One
 * dialog is named below instead: the Media Library's Schedule… opens from
 * state, one dialog for every row's button, and returns focus itself with
 * `onCloseAutoFocus`.
 *
 * WHAT THIS CANNOT SEE. It matches text, and this suite has no DOM, so nothing
 * here watches focus move. It does not see:
 *  - a trigger that is gone or disabled by the time its dialog closes;
 *  - whether a named dialog's `onCloseAutoFocus` focuses anything, which is
 *    why each is named and none passes just by having one;
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

/**
 * Each `components/ui` file built on Radix's Dialog: the root it exports, and
 * the trigger Radix returns focus to.
 */
const OVERLAYS: Record<string, { root: string; trigger: string }> = {
  "dialog.tsx": { root: "Dialog", trigger: "DialogTrigger" },
  "sheet.tsx": { root: "Sheet", trigger: "SheetTrigger" },
};

/**
 * Radix's overlays that return focus to a trigger. A `components/ui` file that
 * wraps one needs a row above.
 */
const RETURNS_TO_TRIGGER =
  /\b(?:AlertDialog|Dialog|DropdownMenu|Popover) as \w+[^}]*\}\s*from\s*["']radix-ui["']/;

/** The dialogs that open from state and return focus themselves, with `onCloseAutoFocus`. */
const RETURNS_FOCUS_ITSELF = ["components/dashboard/media/schedule-dialog.tsx"];

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
  return [...source.matchAll(new RegExp(`<${root}(?=[\\s>])`, "g"))].map(({ index }) => {
    const end = source.indexOf(`</${root}>`, index);
    if (end < 0) throw new Error(`a <${root}> that never closes`);
    return source.slice(index, end);
  });
}

/**
 * The roots in a source that give focus nowhere to go back to. Only a named
 * source may answer with a handler.
 */
function unanchored(source: string, named = false): string[] {
  return Object.values(OVERLAYS).flatMap(({ root, trigger }) =>
    rootsOf(source, root)
      .filter((body) => !body.includes(`<${trigger}`))
      .filter((body) => !(named && body.includes("onCloseAutoFocus=")))
      .map(() => root),
  );
}

describe("every dialog has somewhere for focus to go back to", () => {
  it("finds the tree's dialogs, so an empty search cannot pass as a clean one", () => {
    const withRoots = sources(SRC)
      .filter(({ text }) =>
        Object.values(OVERLAYS).some(({ root }) => rootsOf(text, root).length > 0),
      )
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

  it("reads the roots of each `components/ui` file that wraps a Radix dialog, popover or menu", () => {
    const wrappers = sources(path.join(SRC, "components", "ui"))
      .filter(({ text }) => RETURNS_TO_TRIGGER.test(text))
      .map(({ file }) => path.basename(file));
    expect(wrappers.sort()).toEqual(Object.keys(OVERLAYS).sort());
  });

  it("holds across the tree", () => {
    const found = sources(SRC).flatMap(({ file, text }) =>
      unanchored(text, RETURNS_FOCUS_ITSELF.includes(file)).map((root) => `${file}: <${root}>`),
    );
    expect(found).toEqual([]);
  });

  it("names no dialog that has since been given a trigger", () => {
    for (const named of RETURNS_FOCUS_ITSELF) {
      const text = readFileSync(path.join(SRC, named), "utf8");
      expect(unanchored(text), `${named} no longer opens from state`).not.toEqual([]);
    }
  });

  it("reports a dialog that opens from state, unless it is named and has its handler", () => {
    const opened = (inside: string, root = "Dialog") =>
      `<${root} open={open} onOpenChange={setOpen}>${inside}</${root}>`;
    expect(unanchored(opened("<DialogContent />"))).toEqual(["Dialog"]);
    expect(unanchored(opened("<SheetContent />", "Sheet"))).toEqual(["Sheet"]);
    expect(
      unanchored(opened("<DialogTrigger asChild><Button /></DialogTrigger><DialogContent />")),
    ).toEqual([]);
    // A handler answers for a named dialog only: one that focuses nothing is this same fault.
    expect(unanchored(opened("<DialogContent onCloseAutoFocus={back} />"))).toEqual(["Dialog"]);
    expect(unanchored(opened("<DialogContent onCloseAutoFocus={back} />"), true)).toEqual([]);
    expect(unanchored(opened("<DialogContent />"), true)).toEqual(["Dialog"]);
    // A trigger beside the dialog is not its trigger: Radix reads it from inside the root.
    expect(unanchored(`<DialogTrigger />${opened("<DialogContent />")}`)).toEqual(["Dialog"]);
    expect(() => unanchored("<Dialog open={open} />")).toThrow();
  });
});
