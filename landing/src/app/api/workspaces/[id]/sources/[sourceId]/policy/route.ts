import {NextRequest,NextResponse} from "next/server";
import {isUuid} from "@/lib/session";
import {passThrough,readJsonBody,refuseCrossSite,requireWorkspace} from "@/lib/route-guards";
import {targetFetch} from "@/lib/target-api";

/**
 * GET and PUT /api/workspaces/[id]/sources/[sourceId]/policy — an Instagram
 * past-posts source's policy. Both answer
 * the API's envelope whole (revision, saved and effective values, bounds,
 * capabilities), and a refusal keeps its reason, status and revision. GET is a
 * member's read; the editor reads it when opened and again after a conflict.
 */
export async function GET(_request:NextRequest,context:{params:Promise<{id:string;sourceId:string}>}) {
 const guard=await requireWorkspace(context);if(guard instanceof NextResponse)return guard;
 const {sourceId}=await context.params;if(!isUuid(sourceId))return NextResponse.json({error:"invalid_source"},{status:400});
 const result=await targetFetch(`/workspaces/${guard.id}/sources/${sourceId}/policy`,guard.token,{resourceMetadata:true});
 if(!result.ok)return passThrough(result);return NextResponse.json(result.data);
}
export async function PUT(request:NextRequest,context:{params:Promise<{id:string;sourceId:string}>}) {
 const refused=refuseCrossSite(request);if(refused)return refused;
 const guard=await requireWorkspace(context);if(guard instanceof NextResponse)return guard;
 const {sourceId}=await context.params;if(!isUuid(sourceId))return NextResponse.json({error:"invalid_source"},{status:400});
 const parsed=await readJsonBody(request);if(parsed instanceof NextResponse)return parsed;
 if(!parsed.raw || typeof parsed.raw!=="object" || Array.isArray(parsed.raw))return NextResponse.json({error:"malformed_body"},{status:400});
 const result=await targetFetch(`/workspaces/${guard.id}/sources/${sourceId}/policy`,guard.token,{method:"PUT",body:JSON.stringify(parsed.raw),resourceMetadata:true});
 if(!result.ok)return passThrough(result);return NextResponse.json(result.data);
}
