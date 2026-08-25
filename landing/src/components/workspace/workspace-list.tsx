"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Check, Loader2 } from "lucide-react";
import type { Workspace } from "@/lib/workspaces";

/**
 * Pick a workspace.
 *
 * The whole row is the control, not a trailing "Open" button. There is one
 * action per row and it applies to the row, so a separate target would add a
 * place to miss without adding a choice.
 *
 * Role is shown only where it constrains what you can do — `member` and `admin`
 * are labelled, `owner` is not. Nearly everyone is the owner of nearly every
 * workspace they see, so labelling it would put a badge on every row that
 * distinguishes nothing.
 *
 * The label sits on the NAME's line rather than under it, which is what keeps
 * the list even. Under the name it makes a labelled row taller than an
 * unlabelled one, and the list scans as ragged — the label is the exception,
 * so it must not be the thing that sets the rhythm. A min-height was tried
 * first and is the wrong fix: it raises the floor without reaching the taller
 * row, so it spends vertical space on every row and still does not level them.
 */
export function WorkspaceList({
  workspaces,
  activeId,
}: {
  workspaces: Workspace[];
  activeId: string | null;
}) {
  const router = useRouter();
  const [selecting, setSelecting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function select(id: string) {
    if (selecting) return;
    setSelecting(id);
    setError(null);
    try {
      const response = await fetch(`/api/workspaces/${id}/select`, {
        method: "POST",
      });
      if (!response.ok) {
        setError("That workspace could not be opened.");
        setSelecting(null);
        return;
      }
      router.push("/dashboard");
      router.refresh();
    } catch {
      setError("That workspace could not be opened.");
      setSelecting(null);
    }
  }

  return (
    <div className="space-y-3">
      <ul className="divide-y rounded-lg border bg-card shadow-sm">
        {workspaces.map((workspace) => (
          <li key={workspace.id}>
            <button
              type="button"
              onClick={() => select(workspace.id)}
              disabled={Boolean(selecting)}
              className="flex w-full items-center gap-3 p-4 text-left transition-colors hover:bg-muted/50 disabled:pointer-events-none disabled:opacity-60"
            >
              <span className="truncate font-medium">{workspace.name}</span>
              {workspace.role !== "owner" && (
                <span className="shrink-0 text-sm text-muted-foreground">
                  {workspace.role === "admin" ? "Admin" : "Member"}
                </span>
              )}
              <span className="flex-1" />
              {selecting === workspace.id ? (
                <Loader2 className="h-4 w-4 shrink-0 animate-spin" aria-hidden />
              ) : workspace.id === activeId ? (
                <Check className="h-4 w-4 shrink-0 text-muted-foreground" aria-label="Currently open" />
              ) : null}
            </button>
          </li>
        ))}
      </ul>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
