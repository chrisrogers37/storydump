import { NextRequest, NextResponse } from "next/server";
import {
  passThrough,
  readJsonBody,
  requireWorkspace,
} from "@/lib/route-guards";
import { targetFetch } from "@/lib/target-api";

/**
 * PUT /api/workspaces/[id]/category-mix — replace how often each category
 * posts (owner ruling 2026-09-06). A resource, not a command word: the mix is
 * a table of rows the closed vocabulary has no name for. The API refuses a
 * malformed mix by name (`invalid_mix:<reason>`); this forwards the shape.
 */
export async function PUT(
  request: NextRequest,
  context: { params: Promise<{ id: string }> },
) {
  const guard = await requireWorkspace(context);
  if (guard instanceof NextResponse) return guard;
  const { token, id } = guard;
  const parsedBody = await readJsonBody(request);
  if (parsedBody instanceof NextResponse) return parsedBody;
  const rows = (parsedBody.raw as { rows?: unknown })?.rows;
  if (!Array.isArray(rows)) {
    return NextResponse.json(
      { error: "invalid_mix_not_a_list" },
      { status: 400 },
    );
  }
  const result = await targetFetch<{ rows?: unknown[] }>(
    `/workspaces/${id}/category-mix`,
    token,
    {
      method: "PUT",
      body: JSON.stringify({ rows }),
    },
  );
  if (!result.ok) return passThrough(result);
  return NextResponse.json({
    rows: Array.isArray(result.data?.rows) ? result.data.rows : [],
  });
}
