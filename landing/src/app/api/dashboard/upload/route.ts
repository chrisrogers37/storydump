/**
 * Dedicated upload proxy — forwards multipart file uploads to FastAPI.
 *
 * Separated from the generic BFF proxy because file uploads require
 * streaming the raw body (not JSON re-encoding).
 */

import { NextRequest, NextResponse } from "next/server";
import { getSessionToken, isWorkspaceId, WORKSPACE_COOKIE } from "@/lib/session";
import { TARGET_API_URL } from "@/lib/target-api";

export async function POST(request: NextRequest) {
  const sessionToken = await getSessionToken();
  if (!sessionToken) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }

  const workspaceId = request.cookies.get(WORKSPACE_COOKIE)?.value;
  if (!isWorkspaceId(workspaceId)) {
    return NextResponse.json({ error: "no_workspace_selected" }, { status: 422 });
  }

  // Reject oversized uploads before buffering
  const MAX_UPLOAD_BYTES = 50 * 1024 * 1024; // 50 MB
  const contentLength = parseInt(request.headers.get("content-length") || "0", 10);
  if (contentLength > MAX_UPLOAD_BYTES) {
    return NextResponse.json(
      { error: "File exceeds 50 MB limit" },
      { status: 413 }
    );
  }

  const url = new URL(
    `/api/v1/workspaces/${workspaceId}/upload-media`,
    TARGET_API_URL,
  );

  // Forward the raw multipart body to FastAPI
  const contentType = request.headers.get("content-type") || "";
  const body = await request.arrayBuffer();

  // Double-check actual size after buffering
  if (body.byteLength > MAX_UPLOAD_BYTES) {
    return NextResponse.json(
      { error: "File exceeds 50 MB limit" },
      { status: 413 }
    );
  }

  try {
    const backendResponse = await fetch(url.toString(), {
      method: "POST",
      headers: {
        "Content-Type": contentType,
        Authorization: `Bearer ${sessionToken}`,
      },
      body: body,
    });

    if (backendResponse.status >= 500) {
      console.error("Upload backend 5xx:", backendResponse.status);
      return NextResponse.json(
        { error: "Backend unavailable" },
        { status: 502 }
      );
    }

    const data = await backendResponse.json();
    return NextResponse.json(data, { status: backendResponse.status });
  } catch (error) {
    console.error("Upload proxy error:", error);
    return NextResponse.json(
      { error: "Backend unavailable" },
      { status: 502 }
    );
  }
}
