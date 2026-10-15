/**
 * The create-SUCCESS path. Every case here is a workspace that EXISTS.
 *
 * Three defects this file pins, all downstream of a create that worked:
 *   1. the port answers `workspace_id`, not `id`: a client reading
 *      `workspace.id` puts `undefined` in the select URL, `isWorkspaceId`
 *      refuses it with a 400, no workspace cookie is set, and the person is
 *      bounced out of the workspace they have just made;
 *   2. a deduped create answers `200 {"outcome": "replayed"}` with NO id, which
 *      is not an error and has no destination at all;
 *   3. a throw from a post-create select or `router.push` inside the create's
 *      `try` would render "We could not reach Storydump" over a created
 *      workspace — the worst outcome this flow can produce.
 */

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { destinationAfterCreate } from "./create-workspace-form";

describe("where a person lands after a successful create", () => {
  it("goes to the dashboard when the port's id was read and the select stuck", () => {
    expect(destinationAfterCreate({ outcome: "executed", workspace_id: "ws-1" }, true))
      .toBe("/dashboard");
  });

  it("does NOT read `id` — the port answers `workspace_id`", () => {
    // `id` alone is not the port's shape: code that read `.id` would land on
    // "/workspaces" here only because `workspace_id` is absent, so assert the
    // port's real shape drives it.
    expect(destinationAfterCreate({ id: "ws-1" } as never, true)).toBe("/workspaces");
    expect(destinationAfterCreate({ workspace_id: "ws-1" } as never, true)).toBe("/dashboard");
  });

  it("sends a REPLAY to the workspace list, not nowhere and not an error", () => {
    // `{outcome: "replayed"}` carries no id and cannot be made to carry one.
    expect(destinationAfterCreate({ outcome: "replayed" }, false)).toBe("/workspaces");
  });

  it("does not send anyone to /dashboard without a workspace cookie", () => {
    // The route gate bounces a workspace-less session straight back out, so a
    // create whose select failed lands on the workspace list instead.
    expect(destinationAfterCreate({ workspace_id: "ws-1" }, false)).toBe("/workspaces");
  });

  it("still lands somewhere when the body is unparseable", () => {
    expect(destinationAfterCreate(null, false)).toBe("/workspaces");
  });
});

describe("the outage message is scoped to the CREATE only", () => {
  const src = readFileSync(
    new URL("./create-workspace-form.tsx", import.meta.url),
    "utf8",
  );

  it("returns immediately after reporting an unreachable create", () => {
    // Without the return, control would fall through into the success path.
    const i = src.indexOf("We could not reach Storydump");
    expect(i).toBeGreaterThan(-1);
    expect(src.slice(i, i + 220)).toContain("return;");
  });

  it("does not perform the select inside the block that reports an outage", () => {
    // The select must sit AFTER the create's catch. If it moves inside, a
    // post-create throw reports failure over a created workspace.
    const catchAt = src.indexOf("We could not reach Storydump");
    const selectAt = src.indexOf("/select");
    expect(selectAt).toBeGreaterThan(catchAt);
  });
});
