"use client";

import { useId, useReducer, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Notice } from "@/components/ui/notice";
import { Switch } from "@/components/ui/switch";
import { describedBy } from "@/lib/utils";
import {
  INITIAL_POLICY_FLOW,
  MEMBER_POLICY_COPY,
  POLICY_FIELD_COPY,
  ageBoundsCopy,
  ageCheckCopy,
  ageFactsCopy,
  checkPolicyDraft,
  fetchSourcePolicy,
  policyDraft,
  policyDraftChanged,
  policyDraftStale,
  policyFlowReducer,
  policyValueText,
  saveSourcePolicy,
  type PolicyDraft,
  type PolicyFlow,
  type PolicyField,
} from "@/lib/source-policy";

/**
 * For a button whose label is a sentence. `Button` neither wraps nor shrinks, so
 * at 320px a label such as "Keep my changes on these settings" runs past the
 * card and makes the dashboard scroll sideways. The Posting mix card's review
 * buttons use it too.
 */
export const WRAPPING_BUTTON = "h-auto min-w-0 shrink whitespace-normal py-1.5 text-center";

/**
 * "Past posts settings" under an Instagram source's row in the Posting mix:
 * the minimum age of a post it may pick and the two independent attribution
 * opt-ins.
 *
 * Collapsed until asked for, and read when first opened: a workspace with no
 * one looking at these settings asks the API for none of them. An admin edits
 * through the policy BFF with the schema version and the revision read; a
 * member reads the same values and is told who can change them.
 */
export function SourcePolicyEditor({
  workspaceId,
  sourceId,
  sourceName,
  editable,
}: {
  workspaceId: string;
  sourceId: string;
  /** The source's accessible name ("Instagram past posts @<handle>"), for the toggle's label. */
  sourceName: string;
  editable: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [flow, dispatch] = useReducer(policyFlowReducer, INITIAL_POLICY_FLOW);
  const ids = useId();

  async function load() {
    dispatch({ type: "load_started" });
    dispatch({ type: "loaded", result: await fetchSourcePolicy(workspaceId, sourceId) });
  }

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && (flow.status === "idle" || flow.status === "failed")) void load();
  }

  async function save() {
    if (!flow.draft || !flow.saved) return;
    const check = checkPolicyDraft(flow.draft, flow.saved);
    if (!check.ok) return;
    dispatch({ type: "save_started" });
    const result = await saveSourcePolicy(workspaceId, sourceId, check.body);
    dispatch({ type: "save_answered", result });
    // A conflict keeps the draft; the saved values are read again to sit beside it.
    if (!result.ok && result.error === "policy_conflict") await load();
  }

  return (
    <div className="space-y-2">
      <Button
        type="button"
        variant="link"
        size="sm"
        className="h-auto px-0"
        aria-expanded={open}
        aria-controls={`${ids}-panel`}
        aria-label={`Past posts settings for ${sourceName}`}
        onClick={toggle}
      >
        Past posts settings
      </Button>
      <div id={`${ids}-panel`} hidden={!open}>
        {open && (
          <PolicyPanel
            flow={flow}
            editable={editable}
            ids={ids}
            onEdit={(patch) => dispatch({ type: "edited", patch })}
            onSave={save}
            onRebase={() => dispatch({ type: "rebased" })}
            onDiscard={() => dispatch({ type: "discarded" })}
            onRetry={load}
          />
        )}
      </div>
    </div>
  );
}

/**
 * The panel itself, hook-free so a test can render each state of `flow`
 * without pressing anything (`environment: "node"`).
 */
export function PolicyPanel({
  flow,
  editable,
  ids,
  onEdit,
  onSave,
  onRebase,
  onDiscard,
  onRetry,
}: {
  flow: PolicyFlow;
  editable: boolean;
  ids: string;
  onEdit: (patch: Partial<Omit<PolicyDraft, "basedOn">>) => void;
  onSave: () => void;
  onRebase: () => void;
  onDiscard: () => void;
  onRetry: () => void;
}) {
  const { saved, draft } = flow;
  if (flow.status === "failed" || (flow.status === "loaded" && (!saved || !draft))) {
    return (
      <div className="space-y-2">
        <Notice tone="error">
          {flow.error ?? "These settings could not be loaded just now."}
        </Notice>
        <Button type="button" variant="outline" size="sm" onClick={onRetry}>
          Try again
        </Button>
      </div>
    );
  }
  if (!saved || !draft) {
    return <p className="text-sm text-muted-foreground">Loading past posts settings…</p>;
  }

  const fields = saved.editable;
  const facts = ageFactsCopy(saved);
  // One polite region, always present so each outcome is announced. It is drawn
  // beside Save, under the fields: above them, turning from `sr-only` to a
  // visible line would push the age field and the switches down whenever
  // something is said. Quiet it is absolute and takes no room (`sr-only`).
  const live = (
    <p
      role="status"
      aria-live="polite"
      className={flow.announcement ? "min-w-0 break-words text-sm" : "sr-only"}
    >
      {flow.announcement}
    </p>
  );

  if (!editable) {
    const values = policyDraft(saved);
    return (
      <div className="space-y-2 rounded-md border p-3">
        <dl className="space-y-1 text-sm">
          {fields.map((field) => (
            <div key={field} className="flex flex-wrap gap-x-2">
              <dt className="text-muted-foreground">{POLICY_FIELD_COPY[field].label}</dt>
              <dd className="font-medium">{policyValueText(field, values)}</dd>
            </div>
          ))}
        </dl>
        {fields.includes("minimum_age_days") && (
          <p className="text-xs text-muted-foreground">{facts}</p>
        )}
        <p className="text-sm text-muted-foreground">{MEMBER_POLICY_COPY}</p>
      </div>
    );
  }

  const stale = policyDraftStale(flow);
  const check = checkPolicyDraft(draft, saved);
  const changed = policyDraftChanged(draft, saved);
  const blocked = stale || flow.conflict;
  const ageInvalid = fields.includes("minimum_age_days") && !check.ok;
  const savedValues = policyDraft(saved);

  return (
    <div className="space-y-3 rounded-md border p-3">
      {flow.error && <Notice tone="error">{flow.error}</Notice>}
      {blocked && (
        <div role="group" aria-labelledby={`${ids}-review`} className="space-y-2 rounded-md bg-muted/40 p-3">
          <p id={`${ids}-review`} className="text-sm font-medium">
            {stale
              ? "Your changes, beside the settings saved now"
              : "The saved settings have not been read again yet. Your changes are kept."}
          </p>
          {stale && (
            <ul className="space-y-1 text-sm">
              {fields.map((field) => (
                <li key={field} className="break-words">
                  {POLICY_FIELD_COPY[field].label}: yours {policyValueText(field, draft)} · saved{" "}
                  {policyValueText(field, savedValues)}
                </li>
              ))}
            </ul>
          )}
          <div className="flex flex-wrap gap-2">
            {stale ? (
              <Button
                type="button"
                size="sm"
                variant="outline"
                className={WRAPPING_BUTTON}
                onClick={onRebase}
                disabled={flow.saving}
              >
                Keep my changes on these settings
              </Button>
            ) : (
              <Button
                type="button"
                size="sm"
                variant="outline"
                className={WRAPPING_BUTTON}
                onClick={onRetry}
                disabled={flow.saving}
              >
                Read the saved settings again
              </Button>
            )}
            <Button
              type="button"
              size="sm"
              variant="outline"
              className={WRAPPING_BUTTON}
              onClick={onDiscard}
              disabled={flow.saving}
            >
              Discard my changes
            </Button>
          </div>
        </div>
      )}

      {fields.includes("minimum_age_days") && (
        <div className="space-y-1">
          {/* The repo's `Label`, which is a block of its own: a plain inline
              <label> sits on the narrow input's line with no gap at 768px and up. */}
          <Label htmlFor={`${ids}-age`}>{POLICY_FIELD_COPY.minimum_age_days.label}</Label>
          <Input
            id={`${ids}-age`}
            inputMode="numeric"
            value={draft.minimumAgeDays}
            onChange={(e) => onEdit({ minimumAgeDays: e.target.value })}
            disabled={flow.saving}
            aria-invalid={ageInvalid || undefined}
            aria-describedby={describedBy(
              `${ids}-age-help`,
              `${ids}-age-facts`,
              ageInvalid && `${ids}-age-error`,
            )}
            className="w-32"
          />
          <p id={`${ids}-age-help`} className="text-xs text-muted-foreground">
            {POLICY_FIELD_COPY.minimum_age_days.description} {ageBoundsCopy(saved.ageBounds)}
          </p>
          <p id={`${ids}-age-facts`} className="text-xs text-muted-foreground">
            {facts}
          </p>
          {ageInvalid && (
            <p id={`${ids}-age-error`} className="text-xs text-amber-700">
              {ageCheckCopy(saved.ageBounds)}
            </p>
          )}
        </div>
      )}

      {(["include_credited", "include_unknown"] as const)
        .filter((field) => fields.includes(field))
        .map((field) => (
          <OptIn
            key={field}
            field={field}
            ids={ids}
            checked={field === "include_credited" ? draft.includeCredited : draft.includeUnknown}
            disabled={flow.saving}
            onChange={(next) =>
              onEdit(field === "include_credited" ? { includeCredited: next } : { includeUnknown: next })
            }
          />
        ))}

      {fields.includes("carousel_slides") && saved.carouselChoices.length > 0 && (
        <div className="space-y-1">
          <Label htmlFor={`${ids}-carousel`}>{POLICY_FIELD_COPY.carousel_slides.label}</Label>
          <select
            id={`${ids}-carousel`}
            className="w-full rounded-md border bg-background px-3 py-2 text-sm"
            value={draft.carouselSlides ?? ""}
            disabled={flow.saving}
            onChange={(event) => onEdit({ carouselSlides: event.target.value as "all" | "first" })}
          >
            {saved.carouselChoices.map((choice) => (
              <option key={choice} value={choice}>{choice === "all" ? "All slides" : "First slide only"}</option>
            ))}
          </select>
          <p className="text-xs text-muted-foreground">{POLICY_FIELD_COPY.carousel_slides.description}</p>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          size="sm"
          onClick={onSave}
          disabled={flow.saving || blocked || !changed || !check.ok}
        >
          {flow.saving ? "Saving..." : "Save settings"}
        </Button>
        {changed && !blocked && (
          <span className="text-xs text-muted-foreground">Unsaved changes</span>
        )}
        {live}
      </div>
    </div>
  );
}

function OptIn({
  field,
  ids,
  checked,
  disabled,
  onChange,
}: {
  field: Exclude<PolicyField, "minimum_age_days">;
  ids: string;
  checked: boolean;
  disabled: boolean;
  onChange: (next: boolean) => void;
}) {
  const copy = POLICY_FIELD_COPY[field];
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0 space-y-0.5">
        <p id={`${ids}-${field}-label`} className="text-sm font-medium">
          {copy.label}
        </p>
        <p id={`${ids}-${field}-help`} className="text-xs text-muted-foreground">
          {copy.description}
        </p>
      </div>
      <Switch
        checked={checked}
        disabled={disabled}
        onCheckedChange={onChange}
        aria-labelledby={`${ids}-${field}-label`}
        aria-describedby={`${ids}-${field}-help`}
      />
    </div>
  );
}
