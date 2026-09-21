import { getSessionToken } from "@/lib/session";
import { requireWorkspacePage } from "@/lib/page-guards";
import { targetFetch } from "@/lib/target-api";
import { workspaceFetch } from "@/lib/workspaces";
import {
  deriveSettings,
  type AccountsResponse,
  type DriveStatusResponse,
  type SourcesResponse,
  type StatsResponse,
  type WorkspaceConfig,
} from "@/lib/dashboard-payloads";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { GeneralTab } from "@/components/dashboard/settings/general-tab";
import { AccountsTab } from "@/components/dashboard/settings/accounts-tab";
import type { BindingsResponse, MembersResponse } from "@/lib/types";
import { MembersCard } from "@/components/dashboard/settings/members-card";
import { CategoryWeightsCard } from "@/components/dashboard/settings/category-weights-card";
import type { CategoryMixResponse } from "@/lib/category-mix";
import { IntegrationsTab } from "@/components/dashboard/settings/integrations-tab";
import { ApiTokensTab } from "@/components/dashboard/settings/api-tokens-tab";
import { Notice } from "@/components/ui/notice";
import { tokenRowsFrom } from "@/lib/tokens";

/**
 * Settings — every tab writes. General is the command client (P3), Accounts
 * adds and removes destinations (#1089, and `disable_account`), Integrations
 * connects and disconnects Drive and mints Telegram links, API tokens mints
 * and revokes.
 *
 * It used to ask for `init`, a route that does not exist and is not planned, so
 * the hard bail below fired on EVERY load and this screen rendered
 * `RouterUnavailable` every time — taking Accounts and Integrations with it.
 * "Router unavailable" also misdiagnosed: the router was fine; this page was
 * asking it for something it never served.
 *
 * ── `editable` is GONE, and what replaced it ───────────────────────────
 *
 * It marked controls whose ROUTE did not exist yet. Every one of them now
 * either exists (the General writes are on the command client, epic P3;
 * `disconnect_account` is a built `COMMAND_SPECS` row and Integrations calls
 * it; `sync_now` is wired) or has been removed with the door behind it
 * (#1338 deleted the switch-account control and its BFF proxy). A
 * permanently-true flag is the mirror image of the permanently-false one
 * #1070 refused, and neither survives a reader asking what it would take to
 * flip it.
 *
 * Both connect flows targeted `oauth-url/<provider>` and were DELETED rather
 * than gated (#1070). The Drive one is BACK since 069 (#1165): per-workspace
 * against a per-workspace route, which is the shape it always wanted. That
 * deletion is what made the flag safe to drop rather than flip: it gated ONE
 * kind of thing, controls pending and coming back, so it never held anything
 * that was removed as invalid.
 *
 * The one flag left on this screen is `CategoryWeightsCard`'s
 * `editable={isAdmin}`, which is a different question entirely: a ROLE
 * floor, variable per person, answered from the session's membership.
 *
 * ── There is no page-level read-only banner, deliberately ──────────────────
 *
 * There was one, and it made a single claim about three tabs. That was true
 * while all three were read-only and became false the moment one was not.
 * Now none of them is, and the banner would be wrong about all four. Where a
 * single control is still held off, the tab that owns it says so at the
 * control — which is where a reader meets it; a page-level restatement could
 * only ever be a coarser copy of that, and one more thing to keep true.
 *
 * ── The bail stays hard, and it still is not the whole guard ────────────────
 *
 * A partial render here is actively harmful: a toggle drawn from a default
 * rather than from the workspace shows the wrong current value, and someone
 * will change it to match what they see. That reasoning is unchanged.
 *
 * But the bail only ever caught a fetch that FAILED. A field missing from a
 * response that SUCCEEDED walks straight past it, which is how `?? false`
 * rendered "Auto-sync disabled" as a fact about a workspace from a column that
 * does not exist. `deriveSettings` is the other half of that guard: it resolves
 * every field once and leaves the unsourced ones `Unavailable`.
 */
export default async function SettingsPage({
  searchParams,
}: {
  searchParams: Promise<{ tab?: string; connected?: string }>;
}) {
  // `auth.py:364` has always redirected here with `?connected=gdrive` on a
  // successful Drive grant. This page took NO searchParams at all, so the
  // parameter was unreadable by construction and the grant completed in
  // silence — #1090 B3's other half.
  const { connected } = await searchParams;
  const { session, workspaceId } = await requireWorkspacePage();

  // The name comes from the session's own workspace list rather than a fresh
  // read: the switcher and header already render from it, so a second source
  // here could disagree with them on the same screen. `workspaces` is nullable
  // when the router could not be reached (`session.ts` keeps that distinct from
  // "you have none"), and the empty string is a safe seed — the Save button is
  // disabled on a blank name, so an unreachable read cannot submit one.
  const membership = session.workspaces?.find((w) => w.id === workspaceId);
  const workspaceName = membership?.name ?? "";
  // The Delete / Restore card is owner-only (#1127). An unknown role — the
  // list was unreachable — hides it: a delete control whose refusal we cannot
  // predict is worse than a missing one, and the port refuses non-owners anyway.
  const isOwner = membership?.role === "owner";
  // Service identities are minted, listed and revoked by admins and owners
  // (CLI v2 spec §2). A member is not shown that card at all: the API
  // answers them 403, and a card whose every control is refused is worse
  // than none. An unknown role — the list was unreachable — hides it too.
  const isAdmin = membership?.role === "owner" || membership?.role === "admin";

  const [
    configResult,
    accountsResult,
    sourcesResult,
    bindingsResult,
    membersResult,
    statsResult,
    driveResult,
    mixResult,
    personalTokensResult,
    serviceTokensResult,
  ] = await Promise.all([
    workspaceFetch<WorkspaceConfig>("", workspaceId),
    workspaceFetch<AccountsResponse>("accounts", workspaceId),
    workspaceFetch<SourcesResponse>("sources", workspaceId),
    workspaceFetch<BindingsResponse>("bindings", workspaceId),
    workspaceFetch<MembersResponse>("members", workspaceId),
    workspaceFetch<StatsResponse>("stats", workspaceId),
    workspaceFetch<DriveStatusResponse>("drive", workspaceId),
    workspaceFetch<CategoryMixResponse>("category-mix", workspaceId),
    // The one TENANT-LESS read on this page: a person's tokens are theirs,
    // not a workspace's, so it is `targetFetch` with the session token
    // rather than `workspaceFetch`. Non-critical, like `drive`: a failed
    // read is a null the card names, never a bail.
    targetFetch<{ tokens?: unknown }>("/me/tokens", await getSessionToken()),
    // Service identities are read only for an admin or owner. A member is
    // answered 403, and asking for a refusal to render its own absence is a
    // wasted call; `null` here is "not asked", which the card never sees
    // because it is not rendered for a member.
    isAdmin
      ? workspaceFetch<{ tokens?: unknown }>("tokens", workspaceId)
      : Promise.resolve(null),
  ]);

  // All four, for the reason above: every tab on this screen renders current
  // state, so any one of them missing means some control shows a value that is
  // not the workspace's.
  if (
    !configResult.ok ||
    !accountsResult.ok ||
    !sourcesResult.ok ||
    !statsResult.ok
  ) {
    return <RouterUnavailable what="Settings" />;
  }

  // The grant is one read; a failed read is a null the card names, not a
  // redirect — every other tab stands without it.
  const drive = driveResult.ok ? driveResult.data.drive : null;
  // `?tab=` opens a tab directly (the Media page links to Integrations), and
  // the Drive callback's `?connected=gdrive` lands where the grant is shown.
  const params = await searchParams;
  const initialTab =
    params.tab === "integrations" || params.connected === "gdrive"
      ? "integrations"
      : params.tab === "accounts"
        ? "accounts"
        : params.tab === "tokens"
          ? "tokens"
          : "general";
  const settings = deriveSettings(
    configResult.data,
    sourcesResult.data.sources ?? [],
    statsResult.data,
    drive,
  );
  const accounts = accountsResult.data.accounts ?? [];
  // `tokenRowsFrom` is the same reshape the proxy applies, so the tab sees
  // one row shape whether a list came from this read or from the browser.
  // A list that is not a list is `null` — "could not be loaded" — not `[]`.
  const personalTokens = personalTokensResult.ok
    ? tokenRowsFrom(personalTokensResult.data?.tokens)
    : null;
  const serviceTokens = serviceTokensResult?.ok
    ? tokenRowsFrom(serviceTokensResult.data?.tokens)
    : null;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Your posting schedule, accounts, integrations, and API tokens.
        </p>
      </div>

      {/*
        SAYS ONLY WHAT THE REDIRECT SUBSTANTIATES, which is less than it is
        tempting to write.

        Reaching this parameter means `store_credential` committed — the
        callback redirects to /auth/error otherwise — so "the grant completed"
        is a fact. What is NOT a fact is that media will arrive: the callback
        re-arms a sync only when the state row carried a `reconnect_target`,
        and this parameter cannot say whether it did. An earlier draft of this
        banner promised "media will appear as it syncs, which can take a few
        minutes", which would be a confident wrong statement for every connect
        without a target — the same defect as the error page's, in the other
        direction.

        So it resolves the ambiguity it CAN resolve. During the sync window a
        successful grant and a failed one both show an empty library; this says
        the grant is not the problem, and points at the source's own state,
        which is the signal that actually tracks syncing.
      */}
      {connected === "instagram" && (
        <Notice tone="success">
          <span className="font-medium">Instagram account connected.</span>{" "}
          Storydump will keep its access fresh. Publishing through Instagram
          directly is not switched on yet; approvals still post by hand.
        </Notice>
      )}

      {connected === "gdrive" && (
        <Notice tone="success">
          <span className="font-medium">Google Drive access was granted.</span>{" "}
          The grant completed, so an empty library is not a failed connection.
          Its current state is shown on the Google Drive card under
          Integrations.
        </Notice>
      )}

      <Tabs defaultValue={initialTab}>
        <TabsList>
          <TabsTrigger value="general">General</TabsTrigger>
          <TabsTrigger value="accounts">Accounts</TabsTrigger>
          <TabsTrigger value="integrations">Integrations</TabsTrigger>
          <TabsTrigger value="tokens">API tokens</TabsTrigger>
        </TabsList>

        <TabsContent value="general">
          <GeneralTab
            settings={settings}
            workspaceId={workspaceId}
            workspaceName={workspaceName}
            workspaceState={configResult.data.state}
            restorableUntil={configResult.data.restorable_until}
            isOwner={isOwner}
            categoryMix={
              <CategoryWeightsCard
                // Keyed on the server's rows so a refreshed mix REMOUNTS the
                // card rather than being written into its state by an effect.
                // Changing this key discards an in-progress edit, which is the
                // rule the card documents: after a save or a newly connected
                // folder, the numbers on screen are the server's again.
                key={
                  mixResult.ok
                    ? JSON.stringify(mixResult.data?.rows ?? null)
                    : "unavailable"
                }
                workspaceId={workspaceId}
                data={
                  mixResult.ok && Array.isArray(mixResult.data?.rows)
                    ? mixResult.data
                    : null
                }
                editable={isAdmin}
              />
            }
            members={
              <MembersCard
                workspaceId={workspaceId}
                members={
                  membersResult.ok ? (membersResult.data.members ?? []) : null
                }
                currentUserId={session.userId}
                canRemove={
                  membership?.role === "owner" || membership?.role === "admin"
                }
              />
            }
          />
        </TabsContent>

        {/*
          No `editable` here any more (TD-D3): the one control it gated was
          "Make Active", which POSTed through a BFF proxy onto a target path
          that does not exist. Both are deleted. Connect and Remove are real
          and ungated inside the tab (Remove = `disable_account`, owner
          decision 2026-09-04), so the tab has nothing left that is pending.
        */}
        <TabsContent value="accounts">
          <AccountsTab accounts={accounts} workspaceId={workspaceId} />
        </TabsContent>

        {/*
          No `editable` here either, and by the time it was deleted it had
          nothing left to gate: Connect, the folder picker, Sync Now, Remove
          and Disconnect all call routes that exist (069, #1165), and gating
          them would be the reads-without-writes harm inverted, hiding
          controls that work.
        */}
        <TabsContent value="integrations">
          <IntegrationsTab
            settings={settings}
            sources={sourcesResult.data.sources ?? []}
            drive={drive}
            bindings={
              bindingsResult.ok ? (bindingsResult.data.bindings ?? []) : null
            }
            workspaceId={workspaceId}
            telegramLinked={session.telegramLinked}
            telegramDisplayName={session.telegramDisplayName}
          />
        </TabsContent>

        {/*
          API tokens (CLI v2 phase 01, spec §2): the first step of the
          first-time clock. Every write on this tab is REST at the proxy —
          a token is a resource, not a command — and every read above is
          non-critical, so the tab stands when a list could not be loaded
          and says so on the card.
        */}
        <TabsContent value="tokens">
          <ApiTokensTab
            personalTokens={personalTokens}
            serviceTokens={serviceTokens}
            workspaceId={workspaceId}
            isAdmin={isAdmin}
          />
        </TabsContent>
      </Tabs>
    </div>
  );
}
