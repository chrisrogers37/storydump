/**
 * Sign-out lands where what happened says it should.
 *
 * Asserted without a DOM, per this suite's `environment: "node"`: the button is
 * called as a function with its hooks stubbed, and its `onClick` is invoked
 * against a scripted `fetch`. A sign-out the route refused (a 403 from the
 * cross-site check, a 5xx) or that never left the browser (offline) leaves the
 * session live, so the button stays put and says so instead of landing on
 * `/login` as if it worked.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";

const router = { push: vi.fn(), refresh: vi.fn() };
const state = { status: "idle", set: vi.fn() };

vi.mock("next/navigation", () => ({ useRouter: () => router }));
vi.mock("react", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react")>()),
  useState: () => [state.status, state.set],
}));

const { SignOutButton, SIGNOUT_FAILED, signOutOutcome } = await import(
  "./sign-out-button"
);

type Button = ReactElement<{
  onClick: () => Promise<void>;
  children: unknown;
  disabled: boolean;
}>;

function landed(url: string, ok = true) {
  return { ok, redirected: true, url } as Response;
}

async function click(everywhere: boolean, answer: () => Promise<Response>) {
  vi.stubGlobal("fetch", vi.fn(answer));
  const button = SignOutButton({ everywhere }) as Button;
  await button.props.onClick();
}

beforeEach(() => {
  router.push.mockClear();
  router.refresh.mockClear();
  state.status = "idle";
  state.set.mockClear();
});

describe("signOutOutcome", () => {
  it("reads a redirect to /login as done", () => {
    expect(signOutOutcome(landed("https://storydump.app/login"))).toBe("done");
  });

  it("reads the route's notice redirect as that notice", () => {
    for (const notice of ["incomplete", "unconfirmed", "stale"]) {
      expect(
        signOutOutcome(landed(`https://storydump.app/login?signout=${notice}`)),
      ).toEqual({ notice });
    }
  });

  it("reads a refusal, a server error or no answer as failed", () => {
    expect(signOutOutcome(new Response(null, { status: 403 }))).toBe("failed");
    expect(signOutOutcome(new Response(null, { status: 503 }))).toBe("failed");
    expect(signOutOutcome(null)).toBe("failed");
  });
});

describe("SignOutButton", () => {
  it("lands on /login after a sign-out that worked", async () => {
    await click(true, async () => landed("https://storydump.app/login"));
    expect(router.push).toHaveBeenCalledWith("/login");
    expect(state.set.mock.calls).toEqual([["pending"]]);
  });

  it("lands on the incomplete notice when other devices stayed signed in", async () => {
    await click(true, async () =>
      landed("https://storydump.app/login?signout=incomplete"),
    );
    expect(router.push).toHaveBeenCalledWith("/login?signout=incomplete");
  });

  it("lands on the route's notice even from a page with its own redirect", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => landed("https://storydump.app/login?signout=unconfirmed")),
    );
    const button = SignOutButton({ redirectTo: "/join/abc" }) as Button;
    await button.props.onClick();
    expect(router.push).toHaveBeenCalledWith("/login?signout=unconfirmed");
  });

  it("stays put and says so when the route refuses", async () => {
    await click(true, async () => new Response(null, { status: 403 }));
    expect(router.push).not.toHaveBeenCalled();
    expect(state.set.mock.calls).toEqual([["pending"], ["failed"]]);
  });

  it("stays put and says so when the browser is offline", async () => {
    await click(false, async () => {
      throw new TypeError("Failed to fetch");
    });
    expect(router.push).not.toHaveBeenCalled();
    expect(state.set).toHaveBeenLastCalledWith("failed");
  });

  it("shows the failure in place of its label", () => {
    state.status = "failed";
    const button = SignOutButton({ children: "Sign out" }) as Button;
    expect(button.props.children).toBe(SIGNOUT_FAILED);
    expect(button.props.disabled).toBe(false);
  });

  it("cannot be clicked again while its request is in flight", () => {
    state.status = "pending";
    const button = SignOutButton({ everywhere: true, children: "Sign out" }) as Button;
    expect(button.props.disabled).toBe(true);
    expect(button.props.children).toBe("Sign out");
  });
});
