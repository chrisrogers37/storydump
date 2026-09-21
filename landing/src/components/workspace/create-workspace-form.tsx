"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Loader2 } from "lucide-react";

import { callBff, postJson } from "@/lib/bff";
import { createWorkspaceRefusalCopy } from "@/lib/refusal-copy";
import { WORKSPACE_NAME_MAX } from "@/lib/workspace-name";

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
export function CreateWorkspaceForm({ autoFocus = false }: { autoFocus?: boolean }) {
  const router = useRouter();
  const [name, setName] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trimmed = name.trim();
  const tooLong = trimmed.length > WORKSPACE_NAME_MAX;
  const canSubmit = trimmed.length > 0 && !tooLong && !pending;

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;

    setPending(true);
    setError(null);

    // THIS BLOCK COVERS THE CREATE ONLY. Everything after it runs in a world
    // where the workspace exists, and nothing there may report a failure to the
    // person: telling someone the create failed when it succeeded is the worst
    // outcome this flow has, and it is a scoping bug rather than a wording one.
    // `callBff` does not throw, so the scope is now the `if` rather than a
    // `try` — the boundary is the same one and the early `return` holds it.
    const createResult = await callBff("/api/workspaces", postJson({ name: trimmed }));

    if (!createResult.ok) {
      // Status 0 is the browser's own `fetch` throwing, which used to arrive in
      // a `catch` with this sentence. `createWorkspaceRefusalCopy` has no arm
      // for it — its default reads "That did not work. This one is on us.",
      // which blames the person for an outage — so the sentence stays here.
      setError(
        createResult.status === 0
          ? "We could not reach Storydump. This one is on us."
          : createWorkspaceRefusalCopy(createResult.error, createResult.status),
      );
      setPending(false);
      return;
    }

    const created: { outcome?: string; workspace_id?: string } =
      createResult.data;

    // ── The workspace exists from here down. ────────────────────────────────
    //
    // The port answers `workspace_id`, not `id` — `_render` spreads the command
    // result, and `create_workspace` returns `{"workspace_id": …}`. Reading `id`
    // put `undefined` in the select URL, which `isWorkspaceId` refused with a
    // 400, so no workspace cookie was set and the person was bounced back out of
    // a workspace that had just been created for them.
    //
    // A REPLAY carries no id at all: a deduped command answers
    // `200 {"outcome": "replayed"}` and nothing else. That is not an error — the
    // workspace is real — so it needs a destination rather than a failure, and
    // the id it would need cannot be recovered from the response.
    const workspaceId = created.workspace_id;
    let selected = false;

    if (workspaceId) {
      // Selection is a cookie convenience. Losing it costs a click, not data —
      // and `callBff` reports an unreachable app as `ok: false` rather than
      // throwing, so the swallowed `catch` is now simply this `.ok`.
      const select = await callBff(
        `/api/workspaces/${workspaceId}/select`,
        postJson({}),
      );
      selected = select.ok;
    }

    // `/dashboard` only when the cookie is actually set: the route gate sends a
    // workspace-less session straight back out, so pushing there hopefully is
    // how the original bounce looked to the user. `/workspaces` lists what they
    // have and always renders.
    router.push(destinationAfterCreate(created, selected));
    router.refresh();
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
          maxLength={WORKSPACE_NAME_MAX}
          onChange={(event) => setName(event.target.value)}
          placeholder="e.g. Northside Coffee"
          aria-invalid={tooLong || undefined}
          aria-describedby={error || tooLong ? "workspace-name-error" : undefined}
          className="w-full rounded-md border bg-background px-3 py-2 text-sm outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring"
        />
        {(tooLong || error) && (
          <p id="workspace-name-error" role="alert" className="text-sm text-destructive">
            {tooLong
              ? `Keep it to ${WORKSPACE_NAME_MAX} characters or fewer.`
              : error}
          </p>
        )}
      </div>

      <button
        type="submit"
        disabled={!canSubmit}
        className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:pointer-events-none disabled:opacity-50"
      >
        {pending && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
        {pending ? "Creating…" : "Create workspace"}
      </button>
    </form>
  );
}

/**
 * Where a person goes once the create has SUCCEEDED.
 *
 * Extracted because all three of this flow's defects live in this decision and
 * none of them is visible from the component: the port answers `workspace_id`
 * (not `id`), a deduped create answers `{outcome: "replayed"}` with no id at
 * all, and `/dashboard` bounces a session whose workspace cookie was never set.
 *
 * `/workspaces` is the fallback rather than an error because by this point the
 * workspace EXISTS. It lists what they have and always renders.
 */
export function destinationAfterCreate(
  created: { outcome?: string; workspace_id?: string } | null,
  selected: boolean,
): "/dashboard" | "/workspaces" {
  return created?.workspace_id && selected ? "/dashboard" : "/workspaces";
}
