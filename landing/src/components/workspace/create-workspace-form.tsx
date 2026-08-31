"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Loader2 } from "lucide-react";

import { createWorkspaceRefusalCopy } from "@/lib/refusal-copy";

/**
 * Name a workspace and create it.
 *
 * ONE FIELD. Every other column on `workspaces` has a usable default in the
 * schema — timezone, posting hours, posts per day, approval mode — and asking
 * for them here would be asking someone to make six decisions about a product
 * they have not used yet. They are all in settings, where the answers will
 * mean something.
 *
 * The failure states are separated because their remedies are opposite:
 * `invalid_name` is the person's to fix and belongs under the field;
 * `target_router_unreachable` is not theirs at all, and telling them to try
 * again would be a lie. Neither is rendered as a form-level "something went
 * wrong", which is the phrasing that makes both look like the user's fault.
 */
export function CreateWorkspaceForm({
  submitLabel = "Create workspace",
  autoFocus = false,
}: {
  submitLabel?: string;
  autoFocus?: boolean;
}) {
  const router = useRouter();
  const [name, setName] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trimmed = name.trim();
  const tooLong = trimmed.length > 100;
  const canSubmit = trimmed.length > 0 && !tooLong && !pending;

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;

    setPending(true);
    setError(null);

    try {
      const response = await fetch("/api/workspaces", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: trimmed }),
      });

      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        setError(createWorkspaceRefusalCopy(body?.error, response.status));
        setPending(false);
        return;
      }

      const workspace = await response.json();
      await fetch(`/api/workspaces/${workspace.id}/select`, { method: "POST" });
      router.push("/dashboard");
      router.refresh();
    } catch {
      setError("We could not reach Storydump. This one is on us.");
      setPending(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-3">
      <div className="space-y-1.5">
        <label htmlFor="workspace-name" className="text-sm font-medium">
          Workspace name
        </label>
        <input
          id="workspace-name"
          name="name"
          value={name}
          autoFocus={autoFocus}
          maxLength={120}
          onChange={(event) => setName(event.target.value)}
          placeholder="e.g. Northside Coffee"
          aria-invalid={tooLong || undefined}
          aria-describedby={error || tooLong ? "workspace-name-error" : undefined}
          className="w-full rounded-md border bg-background px-3 py-2 text-sm outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring"
        />
        {(tooLong || error) && (
          <p id="workspace-name-error" role="alert" className="text-sm text-destructive">
            {tooLong ? "Keep it to 100 characters or fewer." : error}
          </p>
        )}
      </div>

      <button
        type="submit"
        disabled={!canSubmit}
        className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:pointer-events-none disabled:opacity-50"
      >
        {pending && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
        {pending ? "Creating…" : submitLabel}
      </button>
    </form>
  );
}
