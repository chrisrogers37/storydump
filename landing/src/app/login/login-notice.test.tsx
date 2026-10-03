/**
 * The sign-in page's notice: the incomplete sign-out says so, and a refusal
 * still names itself. Read as a returned element tree, without a DOM.
 */

import { describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";

const query = { value: "" };
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(query.value),
}));

const { LoginNotice } = await import("./login-notice");
const { SIGNOUT_INCOMPLETE, LOGIN_ERRORS } = await import("./content");

function notice() {
  return LoginNotice() as ReactElement<{ children: unknown }> | null;
}

describe("LoginNotice", () => {
  it("says the other devices are still signed in", () => {
    query.value = "signout=incomplete";
    expect(notice()?.props.children).toBe(SIGNOUT_INCOMPLETE);
  });

  it("shows nothing on a plain visit", () => {
    query.value = "";
    expect(notice()).toBeNull();
  });

  it("names a refusal from ?error=", () => {
    const [code, copy] = Object.entries(LOGIN_ERRORS)[0];
    query.value = `error=${code}`;
    const children = notice()?.props.children as unknown[];
    expect(children[0]).toBe(copy.lead);
    expect(children[2]).toBe(copy.tail);
  });

  it("ignores an unknown error code", () => {
    query.value = "error=whatever";
    expect(notice()).toBeNull();
  });

  it("ignores any other signout value", () => {
    query.value = "signout=whatever";
    expect(notice()).toBeNull();
  });
});
