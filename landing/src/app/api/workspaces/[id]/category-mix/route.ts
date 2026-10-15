import { NextRequest, NextResponse } from "next/server";
import {
  passThrough,
  readJsonBody,
  refuseCrossSite,
  requireWorkspace,
} from "@/lib/route-guards";
import { targetFetch } from "@/lib/target-api";

/**
 * PUT /api/workspaces/[id]/category-mix — replace how often each category
 * posts. A resource, not a command word: the mix is
 * a table of rows the closed vocabulary has no name for. The API refuses a
 * malformed mix by name (`invalid_mix:<reason>`); this forwards the shape.
 */
export async function PUT(
  request: NextRequest,
  context: { params: Promise<{ id: string }> },
) {
  const refused = refuseCrossSite(request);
  if (refused) return refused;

  const guard = await requireWorkspace(context);
  if (guard instanceof NextResponse) return guard;
  const { token, id } = guard;
  const parsedBody = await readJsonBody(request);
  if (parsedBody instanceof NextResponse) return parsedBody;
  if (parsedBody.raw === null || typeof parsedBody.raw !== "object" || Array.isArray(parsedBody.raw)) {
    return NextResponse.json({error:"malformed_body"},{status:400});
  }
  const result = await targetFetch<Record<string,unknown>>(`/workspaces/${id}/category-mix`,token,{
    method:"PUT",body:JSON.stringify(parsedBody.raw),resourceMetadata:true,
  });
  if (!result.ok) return passThrough(result);
  return NextResponse.json(result.data);
}
