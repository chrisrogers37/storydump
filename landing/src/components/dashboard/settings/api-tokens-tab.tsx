"use client";

import { useId, useState } from "react";
import type { ReactElement } from "react";
import { useRouter } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { TONE_CLASS, type BadgeTone } from "@/components/dashboard/tone";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { CopyButton } from "@/components/setup/copy-button";
import {
  EXPIRY_DAYS_DEFAULT,
  EXPIRY_DAYS_MAX,
  EXPIRY_DAYS_MIN,
  TOKEN_NAME_MAX,
  createdCopy,
  expiryCopy,
  expiryDaysValid,
  isTokenRole,
  lastUsedCopy,
  mintMyToken,
  mintServiceToken,
  mintTokenRefusalCopy,
  revokeMyToken,
  revokeServiceToken,
  revokeTokenRefusalCopy,
  tokenNameValid,
  tokenRowState,
} from "@/lib/tokens";
import type {
  MintedToken,
  TokenRole,
  TokenRow,
  TokenRowState,
} from "@/lib/tokens";

/**
 * Settings › API tokens (CLI v2 phase 01, step 10; spec §2).
 *
 * Two cards. YOUR API TOKENS is every member's: a token that acts as you, in
 * every workspace you belong to, at your role there or the token's, whichever
 * is lower. WORKSPACE SERVICE IDENTITIES is for admins and owners: a token
 * that belongs to the workspace and, in this release, reads only.
 *
 * ── The secret is shown once, and this file is where "once" is enforced ───
 *
 * The API returns the secret in the mint response and never again; it stores
 * a hash. So the secret lives in `useState` here, in the card that minted
 * it, until the person dismisses it or leaves the page — `router.refresh()`
 * after a mint re-reads the list from the server and keeps client state, so
 * the new row appears beneath the block that still shows its secret. The
 * block is HOOK-FREE and exported, so a test can read its element tree and
 * prove the secret renders in exactly one place, only while one is in state.
 *
 * ── Why the list is a server read and the writes are browser calls ────────
 *
 * The page reads both lists with the session cookie, as it reads everything
 * else on this screen, and passes `null` when a read failed so the card can
 * say so rather than render "no tokens" to someone who has six. Mint and
 * revoke go through this tier's proxy routes (`/api/me/tokens`,
 * `/api/workspaces/{ws}/tokens`) — REST, because a token is a resource and
 * not a command — and every refusal is a sentence in a banner, because this
 * app has no toast.
 */

export type TokenBadge = {
  label: string;
  tone: BadgeTone;
};

/**
 * Keyed on the state union rather than a `switch`, so a state without a
 * badge is a compile error. Only `live` is ever green. `revoked` and
 * `expired` share nothing but "not usable": one was a decision, the other a
 * clock, so they keep distinct labels and distinct tones.
 */
const TOKEN_BADGE: Record<TokenRowState, TokenBadge> = {
  live: { label: "Live", tone: "active" },
  expired: { label: "Expired", tone: "attention" },
  revoked: { label: "Revoked", tone: "inert" },
};

export function tokenStateBadge(state: TokenRowState): TokenBadge {
  return TOKEN_BADGE[state];
}

/** What a role lets the token do. An unknown role is shown as itself, not as a guess. */
export function roleCopy(role: string): string {
  switch (role) {
    case "operator":
      return "Operator — reads and writes as you";
    case "readonly":
      return "Read-only";
  }
  return role;
}

/** Said on the card and again where the role would otherwise be chosen. */
export const SERVICE_ROLE_NOTE =
  "Service identities read only; writes are made by people with their own tokens.";

/** The form's submit gate — the same two rules the proxy enforces. */
export function mintFormValid(form: { name: string; days: string }): boolean {
  return tokenNameValid(form.name) && expiryDaysValid(daysFrom(form.days));
}

/** A whole number or NaN: "1.5" and "ninety" are not an expiry. */
function daysFrom(days: string): number {
  const trimmed = days.trim();
  return /^\d+$/.test(trimmed) ? Number(trimmed) : NaN;
}

/**
 * The shown-once block. Hook-free on purpose (see the file comment): the
 * secret's only carrier is the copy control's `value`, so it is never a
 * caption, a title attribute, or a second text node.
 */
export function MintedSecretBlock({
  token,
  onDismiss,
}: {
  token: MintedToken;
  onDismiss: () => void;
}): ReactElement {
  return (
    <div
      role="status"
      className="space-y-2 rounded-md border border-green-200 bg-green-50 p-3"
    >
      <p className="text-sm font-medium text-green-900">
        Token &ldquo;{token.name}&rdquo; minted. Copy it now.
      </p>
      <CopyButton value={token.secret} className="w-full" />
      <p className="text-xs text-green-900">
        Run <code>storydump login</code> and paste this when prompted. It is not
        shown again.
      </p>
      <Button type="button" variant="ghost" size="sm" onClick={onDismiss}>
        I have saved it
      </Button>
    </div>
  );
}

/** The card's secret slot: the block while a secret is in state, nothing otherwise. */
export function secretSlot(
  minted: MintedToken | null,
  onDismiss: () => void,
): ReactElement | null {
  return minted ? (
    <MintedSecretBlock token={minted} onDismiss={onDismiss} />
  ) : null;
}

type MintInput = { name: string; role: TokenRole; expiresInDays: number };

/**
 * The role a person's mint form starts on. Read-only: a token that can
 * approve, pause and resolve is chosen on purpose, never handed out because
 * the dropdown was left alone (the audit of 2026-09-16).
 */
export const DEFAULT_MINT_ROLE: TokenRole = "readonly";

/**
 * The mint form, in a dialog (the `accounts-tab` pattern). The fields reset
 * on open, so a second mint does not start with the first one's name. The
 * dialog closes on submit either way: the banner that reports a refusal
 * lives outside it, and a refusal behind an overlay is one nobody reads.
 */
function MintDialog({
  withRole,
  busy,
  triggerLabel,
  title,
  description,
  onMint,
}: {
  /** A person picks a role; a service identity has one, fixed. */
  withRole: boolean;
  busy: boolean;
  triggerLabel: string;
  title: string;
  description: string;
  onMint: (input: MintInput) => Promise<void>;
}) {
  const ids = useId();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [role, setRole] = useState<TokenRole>(DEFAULT_MINT_ROLE);
  const [days, setDays] = useState(String(EXPIRY_DAYS_DEFAULT));
  const valid = mintFormValid({ name, days });

  function onOpenChange(next: boolean) {
    if (next) {
      setName("");
      setRole(DEFAULT_MINT_ROLE);
      setDays(String(EXPIRY_DAYS_DEFAULT));
    }
    setOpen(next);
  }

  async function submit() {
    if (!valid || busy) return;
    await onMint({
      name: name.trim(),
      role: withRole ? role : "readonly",
      expiresInDays: daysFrom(days),
    });
    setOpen(false);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>
        <Button type="button" size="sm" disabled={busy}>
          {triggerLabel}
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor={`${ids}-name`}>Name</Label>
            <Input
              id={`${ids}-name`}
              value={name}
              maxLength={TOKEN_NAME_MAX}
              placeholder={
                withRole ? "claude-code on my laptop" : "nightly report"
              }
              autoComplete="off"
              onChange={(e) => setName(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              What this token is for — the name is what the audit trail shows.
              Up to {TOKEN_NAME_MAX} characters.
            </p>
          </div>
          {withRole ? (
            <div className="space-y-2">
              <Label htmlFor={`${ids}-role`}>Role</Label>
              <Select
                value={role}
                onValueChange={(value) => {
                  if (isTokenRole(value)) setRole(value);
                }}
              >
                <SelectTrigger id={`${ids}-role`} className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="operator">
                    {roleCopy("operator")}
                  </SelectItem>
                  <SelectItem value="readonly">
                    {roleCopy("readonly")}
                  </SelectItem>
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                Never more than your own role in a workspace, whichever you
                pick.
              </p>
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">{SERVICE_ROLE_NOTE}</p>
          )}
          <div className="space-y-2">
            <Label htmlFor={`${ids}-days`}>Expires in (days)</Label>
            <Input
              id={`${ids}-days`}
              type="number"
              inputMode="numeric"
              min={EXPIRY_DAYS_MIN}
              max={EXPIRY_DAYS_MAX}
              step={1}
              value={days}
              onChange={(e) => setDays(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              {EXPIRY_DAYS_MIN}–{EXPIRY_DAYS_MAX} days. An expired token stops
              working; mint a new one when it does.
            </p>
          </div>
        </div>
        <DialogFooter>
          <DialogClose asChild>
            <Button type="button" variant="outline" disabled={busy}>
              Cancel
            </Button>
          </DialogClose>
          <Button type="button" onClick={submit} disabled={!valid || busy}>
            {busy ? "Minting..." : "Mint"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** One card: the list, the mint control, and the shown-once block. */
function TokenCard({
  title,
  description,
  rows,
  unavailableCopy,
  emptyCopy,
  minted,
  onDismissMinted,
  pending,
  onRevoke,
  mintDialog,
}: {
  title: string;
  description: string;
  /** Null = the read failed; the card says so rather than rendering "none". */
  rows: TokenRow[] | null;
  unavailableCopy: string;
  emptyCopy: string;
  minted: MintedToken | null;
  onDismissMinted: () => void;
  pending: string | null;
  onRevoke: (row: TokenRow) => void;
  mintDialog: ReactElement;
}) {
  const now = new Date();
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
        <CardAction>{mintDialog}</CardAction>
      </CardHeader>
      <CardContent className="space-y-3">
        {secretSlot(minted, onDismissMinted)}
        {rows === null ? (
          <p className="text-sm text-muted-foreground">{unavailableCopy}</p>
        ) : rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">{emptyCopy}</p>
        ) : (
          <ul className="divide-y">
            {rows.map((row) => {
              const state = tokenRowState(row, now);
              const badge = tokenStateBadge(state);
              return (
                <li
                  key={row.id}
                  className="flex flex-wrap items-center justify-between gap-3 py-3"
                >
                  <div className="min-w-0 space-y-0.5">
                    <div className="flex items-center gap-2">
                      <p className="truncate font-medium">{row.name}</p>
                      {/* Unconditional: a row states what it is in every
                          state (#1121). Only `live` is ever green. */}
                      <Badge
                        variant="secondary"
                        className={TONE_CLASS[badge.tone]}
                      >
                        {badge.label}
                      </Badge>
                    </div>
                    <p className="text-sm text-muted-foreground">
                      {roleCopy(row.role)}
                    </p>
                    <p className="text-xs text-muted-foreground">
                      {expiryCopy(row.expiresAt, now)} ·{" "}
                      {lastUsedCopy(row.lastUsedAt, now)} ·{" "}
                      {createdCopy(row.createdAt)}
                    </p>
                  </div>
                  {state === "live" && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => onRevoke(row)}
                      disabled={pending !== null}
                    >
                      {pending === `revoke-${row.id}`
                        ? "Revoking..."
                        : "Revoke"}
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

interface ApiTokensTabProps {
  /** The signed-in person's tokens; null = could not be loaded. */
  personalTokens: TokenRow[] | null;
  /** The workspace's service identities; null = could not be loaded, or not an admin. */
  serviceTokens: TokenRow[] | null;
  workspaceId: string;
  /** Membership role `admin` or `owner` — the floor for service identities. */
  isAdmin: boolean;
}

export function ApiTokensTab({
  personalTokens,
  serviceTokens,
  workspaceId,
  isAdmin,
}: ApiTokensTabProps) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [mintedPersonal, setMintedPersonal] = useState<MintedToken | null>(
    null,
  );
  const [mintedService, setMintedService] = useState<MintedToken | null>(null);

  async function mintPersonal(input: MintInput) {
    setError(null);
    setNotice(null);
    setPending("mint-personal");
    try {
      const result = await mintMyToken(input);
      if (!result.ok) {
        setError(mintTokenRefusalCopy(result.error));
        return;
      }
      setMintedPersonal(result.token);
      router.refresh();
    } finally {
      setPending(null);
    }
  }

  async function mintService(input: MintInput) {
    setError(null);
    setNotice(null);
    setPending("mint-service");
    try {
      const result = await mintServiceToken(workspaceId, {
        name: input.name,
        expiresInDays: input.expiresInDays,
      });
      if (!result.ok) {
        setError(mintTokenRefusalCopy(result.error));
        return;
      }
      setMintedService(result.token);
      router.refresh();
    } finally {
      setPending(null);
    }
  }

  async function revoke(kind: "personal" | "service", row: TokenRow) {
    setError(null);
    setNotice(null);
    setPending(`revoke-${row.id}`);
    try {
      const result =
        kind === "personal"
          ? await revokeMyToken(row.id)
          : await revokeServiceToken(workspaceId, row.id);
      if (!result.ok) {
        setError(revokeTokenRefusalCopy(result.error));
        return;
      }
      setNotice(
        `"${row.name}" revoked. Anything still using it is refused from its next call.`,
      );
      router.refresh();
    } finally {
      setPending(null);
    }
  }

  return (
    <div className="space-y-6 pt-4">
      {error && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
          {error}
        </div>
      )}
      {notice && (
        <div className="mb-4 rounded-md border bg-muted/40 p-3 text-sm">
          {notice}
        </div>
      )}

      <TokenCard
        title="Your API tokens"
        description="A token acts as you, in every workspace you belong to, at your role there at most. Mint one per machine or agent, and revoke it when that machine goes away."
        rows={personalTokens}
        unavailableCopy="Your tokens could not be loaded just now. Reload to try again."
        emptyCopy="No tokens yet. Mint one to use the storydump CLI as yourself."
        minted={mintedPersonal}
        onDismissMinted={() => setMintedPersonal(null)}
        pending={pending}
        onRevoke={(row) => revoke("personal", row)}
        mintDialog={
          <MintDialog
            withRole
            busy={pending !== null}
            triggerLabel="Mint token"
            title="Mint an API token"
            description="The token acts as you. Its secret is shown once, right after minting."
            onMint={mintPersonal}
          />
        }
      />

      {/*
        Admins and owners only. A member is not shown a card whose every
        control the API would refuse; the page decides from the membership
        role, and the API refuses regardless.
      */}
      {isAdmin && (
        <TokenCard
          title="Workspace service identities"
          description={`A service identity belongs to this workspace, not to a person. ${SERVICE_ROLE_NOTE}`}
          rows={serviceTokens}
          unavailableCopy="Service identities could not be loaded just now. Reload to try again."
          emptyCopy="No service identity yet. Mint one for automation that reads this workspace."
          minted={mintedService}
          onDismissMinted={() => setMintedService(null)}
          pending={pending}
          onRevoke={(row) => revoke("service", row)}
          mintDialog={
            <MintDialog
              withRole={false}
              busy={pending !== null}
              triggerLabel="Mint service identity"
              title="Mint a service identity"
              description="A read-only identity for this workspace. Its secret is shown once, right after minting."
              onMint={mintService}
            />
          }
        />
      )}
    </div>
  );
}
