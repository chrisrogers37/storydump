/**
 * The lane a past-posts preview waits in (#1644): at most two in flight
 * for a workspace, first come first served, and a place is given back by
 * whatever ends the wait. The lanes are the module's, shared by every test, so
 * each test names a workspace of its own and gives every place back.
 */

import { afterEach, describe, expect, it } from "vitest";
import { MAX_PREVIEWS_IN_FLIGHT, requestPreviewSlot } from "./preview-limiter";

/** A requester that records the order it was granted in and how many hold a place. */
function lane(workspaceId: string) {
  const granted: number[] = [];
  const releases = new Map<number, () => void>();
  const held = new Set<number>();
  let peak = 0;
  const gives: Array<() => void> = [];

  function request(id: number) {
    const give = requestPreviewSlot(workspaceId, () => {
      granted.push(id);
      held.add(id);
      peak = Math.max(peak, held.size);
    });
    releases.set(id, () => {
      held.delete(id);
      give();
    });
    gives.push(give);
    return id;
  }
  return {
    request,
    release: (id: number) => releases.get(id)!(),
    granted,
    held: () => [...held].sort((a, b) => a - b),
    peak: () => peak,
    /** Gives every place back and withdraws every request. */
    drain() {
      for (const give of gives) give();
      held.clear();
    },
  };
}

let current: ReturnType<typeof lane> | null = null;
const open = (workspaceId: string) => (current = lane(workspaceId));
afterEach(() => current?.drain());

describe("a workspace's lane", () => {
  it("is two", () => {
    expect(MAX_PREVIEWS_IN_FLIGHT).toBe(2);
  });

  it("grants the first two at once and holds the rest, in the order they asked", () => {
    const l = open("ws-lane-order");
    [1, 2, 3, 4, 5].forEach(l.request);
    expect(l.granted).toEqual([1, 2]);
    expect(l.held()).toEqual([1, 2]);
    l.release(1);
    expect(l.granted).toEqual([1, 2, 3]);
    l.release(2);
    l.release(3);
    expect(l.granted).toEqual([1, 2, 3, 4, 5]);
    expect(l.held()).toEqual([4, 5]);
  });

  it("never has more than two in flight, whatever order the places come back in", () => {
    const l = open("ws-lane-peak");
    const ids = Array.from({ length: 12 }, (_, i) => i + 1);
    ids.forEach(l.request);
    // Give back the places in an order that is not the order they were granted.
    const order = [2, 1, 4, 3, 6, 5, 8, 7, 10, 9, 12, 11];
    for (const id of order) {
      if (l.held().includes(id)) l.release(id);
    }
    // Releasing what is held only, until every request has had its turn.
    while (l.held().length > 0) l.release(l.held()[0]);
    expect(l.granted).toEqual(ids);
    expect(l.peak()).toBe(2);
  });

  it("grants at once, before the request returns, when it has a place", () => {
    let grantedBeforeReturn = false;
    const give = requestPreviewSlot("ws-lane-sync", () => {
      grantedBeforeReturn = true;
    });
    expect(grantedBeforeReturn).toBe(true);
    give();
  });

  it("withdraws a request that is still waiting: it is never granted, and it costs no place", () => {
    const l = open("ws-lane-withdraw");
    [1, 2, 3, 4].forEach(l.request);
    // 3 gives up its wait; releasing 1 must go to 4, which is next.
    l.release(3);
    l.release(1);
    expect(l.granted).toEqual([1, 2, 4]);
    expect(l.held()).toEqual([2, 4]);
  });

  it("gives a place back once, however many times it is told to", () => {
    const l = open("ws-lane-twice");
    [1, 2, 3, 4].forEach(l.request);
    l.release(1);
    l.release(1);
    l.release(1);
    // One place came free, so one request was granted: 3, not 3 and 4.
    expect(l.granted).toEqual([1, 2, 3]);
    expect(l.held()).toEqual([2, 3]);
    // And 4 is still waiting its turn, not dropped by the extra calls.
    l.release(2);
    expect(l.granted).toEqual([1, 2, 3, 4]);
  });

  it("keeps each workspace's lane apart", () => {
    const a = lane("ws-lane-a");
    const b = lane("ws-lane-b");
    [1, 2, 3].forEach(a.request);
    [1, 2, 3].forEach(b.request);
    expect(a.granted).toEqual([1, 2]);
    expect(b.granted).toEqual([1, 2]);
    a.release(1);
    expect(a.granted).toEqual([1, 2, 3]);
    // Another workspace's place coming free does not move this one.
    expect(b.granted).toEqual([1, 2]);
    a.drain();
    b.drain();
  });

  it("holds the limit in a lane that went idle and came back, whatever a stale release does", () => {
    const first = lane("ws-lane-idle");
    first.request(1);
    first.release(1);
    // The lane is idle and forgotten. A new one stands in its place.
    const second = open("ws-lane-idle");
    [1, 2, 3].forEach(second.request);
    expect(second.granted).toEqual([1, 2]);
    // The first requester is told to give up again, long after: nothing changes,
    // and a request after it still waits behind the two that hold the lane.
    first.release(1);
    second.request(4);
    expect(second.granted).toEqual([1, 2]);
    expect(second.held()).toEqual([1, 2]);
    second.release(1);
    expect(second.granted).toEqual([1, 2, 3]);
  });

  it("holds the limit when a granted request asks for another from inside its grant", () => {
    const workspace = "ws-lane-reentrant";
    const granted: string[] = [];
    const gives: Array<() => void> = [];
    gives.push(
      requestPreviewSlot(workspace, () => {
        granted.push("a");
        gives.push(requestPreviewSlot(workspace, () => granted.push("b")));
        gives.push(requestPreviewSlot(workspace, () => granted.push("c")));
      }),
    );
    // a holds one place and b the other; c waits.
    expect(granted).toEqual(["a", "b"]);
    gives[0]();
    expect(granted).toEqual(["a", "b", "c"]);
    for (const give of gives) give();
  });
});
