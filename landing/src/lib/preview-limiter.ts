/**
 * How many past-posts previews one workspace has in flight at once: two. Each
 * preview is a fresh lookup at the provider, so a page of them is asked for
 * two at a time and the rest wait their turn; the browser's own lazy loading
 * does not bound that.
 *
 * One lane per workspace, first come first served, in this tab. A tile asks for
 * a place with `requestPreviewSlot` and starts its picture when it is granted;
 * the function it gets back gives the place up, whether the picture arrived,
 * failed or was walked away from, and withdraws a request still waiting.
 */

export const MAX_PREVIEWS_IN_FLIGHT = 2;

type Request = { granted: () => void; state: "waiting" | "held" | "done" };
type Lane = { held: number; waiting: Request[] };

const lanes = new Map<string, Lane>();

/**
 * Asks for a place in the workspace's lane. `granted` runs once, as soon as the
 * workspace has fewer than two previews in flight, which may be before this
 * returns. The result gives the place up, or withdraws the request if it has
 * none yet; calling it again does nothing.
 */
export function requestPreviewSlot(workspaceId: string, granted: () => void): () => void {
  const lane = lanes.get(workspaceId) ?? { held: 0, waiting: [] };
  lanes.set(workspaceId, lane);
  const request: Request = { granted, state: "waiting" };
  lane.waiting.push(request);
  advance(workspaceId, lane);
  return () => {
    if (request.state === "done") return;
    if (request.state === "held") lane.held -= 1;
    else lane.waiting.splice(lane.waiting.indexOf(request), 1);
    request.state = "done";
    advance(workspaceId, lane);
  };
}

/** Grants the lane's waiting requests, in order, while it has places. */
function advance(workspaceId: string, lane: Lane) {
  while (lane.held < MAX_PREVIEWS_IN_FLIGHT && lane.waiting.length > 0) {
    const next = lane.waiting.shift()!;
    next.state = "held";
    lane.held += 1;
    next.granted();
  }
  // An idle lane is dropped; the next request starts another.
  if (lane.held === 0 && lane.waiting.length === 0) lanes.delete(workspaceId);
}
