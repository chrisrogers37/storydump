/**
 * The page shown again after a hand-off (`handoff-return.ts`), driven with
 * Node's own EventTarget standing in for `window` and `document`. Whether a
 * browser sends these events when the person comes back shows only in a
 * browser, which these tests cannot drive.
 */

import { describe, expect, it, vi } from "vitest";
import { onReturnAfterHandoff } from "./handoff-return";

/** A `pageshow`: `persisted` when the back-forward cache restored the page. */
const pageshow = (persisted: boolean) => Object.assign(new Event("pageshow"), { persisted });

/** A page that has handed off, hidden, with a reset armed. */
function handedOff() {
  const win = new EventTarget();
  const doc = Object.assign(new EventTarget(), {
    visibilityState: "hidden" as DocumentVisibilityState,
  });
  const reset = vi.fn();
  const stop = onReturnAfterHandoff(reset, win, doc);
  const become = (state: DocumentVisibilityState) => {
    doc.visibilityState = state;
    doc.dispatchEvent(new Event("visibilitychange"));
  };
  return { win, reset, stop, become };
}

describe("a page shown again after a hand-off", () => {
  it("resets when the back-forward cache restores it, not on a load's pageshow", () => {
    const { win, reset } = handedOff();
    win.dispatchEvent(pageshow(false));
    expect(reset).not.toHaveBeenCalled();
    win.dispatchEvent(pageshow(true));
    expect(reset).toHaveBeenCalledTimes(1);
  });

  it("resets when it becomes visible, not when it is hidden", () => {
    const { reset, become } = handedOff();
    become("hidden");
    expect(reset).not.toHaveBeenCalled();
    become("visible");
    expect(reset).toHaveBeenCalledTimes(1);
  });

  it("resets once, however often and whichever way it is shown again", () => {
    const { win, reset, become } = handedOff();
    win.dispatchEvent(pageshow(true));
    become("visible");
    win.dispatchEvent(pageshow(true));
    expect(reset).toHaveBeenCalledTimes(1);
  });

  it("does nothing once stopped", () => {
    const { win, reset, stop, become } = handedOff();
    stop();
    win.dispatchEvent(pageshow(true));
    become("visible");
    expect(reset).not.toHaveBeenCalled();
  });
});
