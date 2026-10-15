/**
 * The sign-in page's notice: a sign-out that did not fully happen says so, and
 * a refusal still names itself. Read as a returned element tree, without a DOM.
 */

import { describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";

const query = { value: "" };
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(query.value),
}));

const { LoginNotice } = await import("./login-notice");
const { SIGNOUT_NOTICES, LOGIN_ERRORS } = await import("./content");

function notice() {
  return LoginNotice() as ReactElement<{ children: unknown }> | null;
}

describe("LoginNotice", () => {
  it.each(Object.entries(SIGNOUT_NOTICES))(
    "says what a sign-out left undone (%s)",
    (code, copy) => {
      query.value = `signout=${code}`;
      expect(notice()?.props.children).toBe(copy);
    },
  );

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
