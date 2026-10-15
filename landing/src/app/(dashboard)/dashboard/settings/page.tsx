import { getSessionToken, isUuid } from "@/lib/session";
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
import { ApprovalsCard } from "@/components/dashboard/settings/approvals-card";
import { AccountsTab } from "@/components/dashboard/settings/accounts-tab";
import type { BindingsResponse, MembersResponse } from "@/lib/types";
import { MembersCard } from "@/components/dashboard/settings/members-card";
import { hasActiveTelegramGroup } from "@/lib/members";
import { CategoryWeightsCard } from "@/components/dashboard/settings/category-weights-card";
import { SourceMixCard } from "@/components/dashboard/settings/source-mix-card";
import type { CategoryMixResponse } from "@/lib/category-mix";
import { parseSourceMix } from "@/lib/source-mix";
import { IntegrationsTab } from "@/components/dashboard/settings/integrations-tab";
import { ApiTokensTab } from "@/components/dashboard/settings/api-tokens-tab";
import { Notice } from "@/components/ui/notice";
import { tokenRowsFrom } from "@/lib/tokens";
import { InviteMember } from "@/components/dashboard/settings/invite-member";
import { pendingInvitationsFrom } from "@/lib/invitations";
import { SettingsHeader } from "@/components/dashboard/page-headers";
import { ScrollToHash } from "@/components/dashboard/scroll-to-hash";
import { POSTING_MIX_ID } from "@/lib/fragment-scroll";
import { ConnectionResultBanner } from "@/components/dashboard/settings/connection-result-banner";
import { parseConnectionResult } from "@/lib/connection-result";

/**
 * Settings — every tab writes. General is the command client, Accounts
 * adds and removes destinations (and `disable_account`), Integrations
 * connects and disconnects Drive and mints Telegram links, API tokens mints
 * and revokes.
 *
 * ── No `editable` flag ─────────────────────────────────────────────────
 *
 * Every control on these tabs has its route: the General writes are on the
 * command client, `disconnect_account` is a built `COMMAND_SPECS` row and
 * Integrations calls it, `sync_now` is wired, Drive connects per workspace
 * against a per-workspace route (`workspace Drive grant`), and there is no switch-account
 * control. A permanently-true flag is the mirror image of a
 * permanently-false one, and neither survives a reader asking what it
 * would take to flip it.
 *
 * The one flag left on this screen is the posting mix's `editable={isAdmin}`
 * (on `CategoryWeightsCard`, or `SourceMixCard` for a converted workspace),
 * which is a different question entirely: a ROLE floor, variable per person,
 * answered from the session's membership.
 *
 * ── There is no page-level read-only banner, deliberately ──────────────────
 *
 * A banner would make a single claim about every tab, and no tab is
 * read-only. Where a single control is held off, the tab that owns it says
 * so at the control — which is where a reader meets it; a page-level
 * restatement could only ever be a coarser copy of that, and one more thing
 * to keep true.
 *
 * ── The bail is hard, and it is not the whole guard ─────────────────────────
 *
 * A partial render here is actively harmful: a toggle drawn from a default
 * rather than from the workspace shows the wrong current value, and someone
 * will change it to match what they see.
 *
 * But the bail catches only a fetch that FAILED. A field missing from a
 * response that SUCCEEDED walks straight past it, which is how `?? false`
 * would render "Auto-sync disabled" as a fact about a workspace from a column
 * that does not exist. `deriveSettings` is the other half of that guard: it
 * resolves every field once and leaves the unsourced ones `Unavailable`.
 */
export default async function SettingsPage({
  searchParams,
}: {
  searchParams: Promise<{ tab?: string; connected?: string; connection?: string }>;
}) {
  // `auth.py:364` redirects here with `?connected=gdrive` on a successful
  // Drive grant, and this page reads it, so the grant does not complete in
  // silence.
  //
  // `?connection=<id>` is the Instagram callback's, where past posts are a
  // choice: an opaque id, and only its shape is trusted. The
  // banner says what the API answers for it, never what the query says.
  const { connected, connection } = await searchParams;
  const { session, workspaceId } = await requireWorkspacePage();

  // The name comes from the session's own workspace list rather than a fresh
  // read: the switcher and header already render from it, so a second source
  // here could disagree with them on the same screen. `workspaces` is nullable
  // when the router could not be reached (`session.ts` keeps that distinct from
  // "you have none"), and the empty string is a safe seed — the Save button is
  // disabled on a blank name, so an unreachable read cannot submit one.
  const membership = session.workspaces?.find((w) => w.id === workspaceId);
  const workspaceName = membership?.name ?? "";
  // The Delete / Restore card is owner-only. An unknown role — the
  // list was unreachable — hides it: a delete control whose refusal we cannot
  // predict is worse than a missing one, and the port refuses non-owners anyway.
  const isOwner = membership?.role === "owner";
  // Service identities are minted, listed and revoked by admins and owners.
  // A member is not shown that card at all: the API answers them 403, and a
  // card whose every control is refused is worse than none. An unknown role —
  // the list was unreachable — hides it too.
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
    invitationsResult,
    connectionResultRead,
  ] = await Promise.all([
    workspaceFetch<WorkspaceConfig>("", workspaceId),
    workspaceFetch<AccountsResponse>("accounts", workspaceId),
    workspaceFetch<SourcesResponse>("sources", workspaceId),
    workspaceFetch<BindingsResponse>("bindings", workspaceId),
    workspaceFetch<MembersResponse>("members", workspaceId),
    workspaceFetch<StatsResponse>("stats", workspaceId),
    workspaceFetch<DriveStatusResponse>("drive", workspaceId),
    workspaceFetch<CategoryMixResponse & { protocol_version?: unknown }>(
      "category-mix",
      workspaceId,
    ),
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
    // Pending invitations carry the invitees' addresses, and only an
    // admin can invite, so a member's page never asks for them.
    isAdmin
      ? workspaceFetch<{ invitations?: unknown }>("invitations", workspaceId)
      : Promise.resolve(null),
    // Non-critical, like `drive`: a result that is not there, expired or is
    // not this session's is a 404, and no banner.
    isUuid(connection)
      ? workspaceFetch<unknown>(`connection-results/${connection}`, workspaceId)
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
  const sources = sourcesResult.data.sources ?? [];
  const settings = deriveSettings(configResult.data, sources, statsResult.data, drive);
  const accounts = accountsResult.data.accounts ?? [];
  // Read once for two cards: Integrations lists the bindings, and Members
  // reminds that a removed person is still in a bound group. `null` = unread.
  const bindings = bindingsResult.ok
    ? (bindingsResult.data.bindings ?? [])
    : null;
  // `tokenRowsFrom` is the same reshape the proxy applies, so the tab sees
  // one row shape whether a list came from this read or from the browser.
  // A list that is not a list is `null` — "could not be loaded" — not `[]`.
  const personalTokens = personalTokensResult.ok
    ? tokenRowsFrom(personalTokensResult.data?.tokens)
    : null;
  const serviceTokens = serviceTokensResult?.ok
    ? tokenRowsFrom(serviceTokensResult.data?.tokens)
    : null;
  const invitations = invitationsResult?.ok
    ? pendingInvitationsFrom(invitationsResult.data?.invitations)
    : null;
  const connectionResult = connectionResultRead?.ok
    ? parseConnectionResult(connectionResultRead.data)
    : null;
  // The read says which protocol the workspace's mix speaks; nothing here
  // guesses it from the rows. A protocol this page does not know gets neither
  // editor: the protocol-1 card with no rows says the mix could not be loaded,
  // and writes nothing.
  const mixData = mixResult.ok ? mixResult.data : null;
  const mixProtocol = mixData?.protocol_version;
  // A converted workspace's mix, as the cards model it; null for protocol 1 or
  // a read that could not be made. The Posting mix card draws it, and the
  // Integrations tab writes against its revision: a Drive folder picked or
  // removed moves the mix, and the API refuses a write that does not say which
  // revision it was made against.
  const sourceMix = parseSourceMix(mixData);
  const legacyMix =
    mixData &&
    (mixProtocol === undefined || mixProtocol === 1) &&
    Array.isArray(mixData.rows)
      ? mixData
      : null;

  return (
    <div className="space-y-6">
      <SettingsHeader />
      {/* The dashboard scrolls inside <main>, where the browser's own fragment
          scroll does not reach on a document load: a link to `#posting-mix`
          lands here and this scrolls the card into view (`fragment-scroll.ts`). */}
      <ScrollToHash />

      {/*
        SAYS ONLY WHAT THE REDIRECT SUBSTANTIATES, which is less than it is
        tempting to write.

        Reaching this parameter means `store_credential` committed — the
        callback redirects to /auth/error otherwise — so "the grant completed"
        is a fact. What is NOT a fact is that media will arrive: the callback
        re-arms a sync only when the state row carried a `reconnect_target`,
        and this parameter cannot say whether it did. A banner that promised
        "media will appear as it syncs, which can take a few minutes" would be
        a confident wrong statement for every connect without a target — the
        same defect as the error page's, in the other direction.

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

      {connectionResult && <ConnectionResultBanner result={connectionResult} />}

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
              mixProtocol === 2 ? (
                // A converted workspace (protocol 2). NOT keyed, unlike the
                // card below: a refresh after a conflict must hand this card
                // the new read WITHOUT remounting it, or the unsaved draft
                // the conflict kept would be thrown away
                // (`source-mix-card.tsx` says how it keeps one).
                // `id`: the anchor the connection banner, the overview's
                // past-posts remedies and the empty library open at
                // (`POSTING_MIX_HREF`, `RESOLVED_IN.mix`).
                <div id={POSTING_MIX_ID} className="scroll-mt-24">
                  <SourceMixCard
                    workspaceId={workspaceId}
                    data={sourceMix}
                    editable={isAdmin}
                  />
                </div>
              ) : (
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
                  data={legacyMix}
                  editable={isAdmin}
                />
              )
            }
            approvals={
              <ApprovalsCard
                // Not keyed on the positions, as the mix card is on its rows:
                // a remount after each choice would take the focus off the
                // control just used. The card takes the re-read in place.
                workspaceId={workspaceId}
                position={settings.approval_mode}
                sources={sources}
                apiPublishing={settings.enable_instagram_api}
                // Positions are an admin's, held for a member like the
                // mix card's weights.
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
                canRemove={isAdmin}
                telegramGroupLinked={hasActiveTelegramGroup(bindings)}
              >
                {isAdmin && (
                  <InviteMember
                    workspaceId={workspaceId}
                    invitations={invitations}
                    tz={configResult.data.tz ?? "UTC"}
                  />
                )}
              </MembersCard>
            }
          />
        </TabsContent>

        {/*
          No `editable` here: Connect and Remove are real and ungated inside
          the tab (Remove = `disable_account`), so the tab has nothing that is
          pending.
        */}
        <TabsContent value="accounts">
          <AccountsTab accounts={accounts} workspaceId={workspaceId} />
        </TabsContent>

        {/*
          No `editable` here either: Connect, the folder picker, Sync Now,
          Remove and Disconnect all call routes that exist (`workspace Drive grant`), and
          gating them would be the reads-without-writes harm inverted, hiding
          controls that work. `isAdmin` gates one control only: removing a
          Telegram group, which the API refuses below the admin floor.
        */}
        <TabsContent value="integrations">
          <IntegrationsTab
            settings={settings}
            sources={sources}
            drive={drive}
            mix={sourceMix}
            bindings={bindings}
            workspaceId={workspaceId}
            telegramLinked={session.telegramLinked}
            telegramDisplayName={session.telegramDisplayName}
            isAdmin={isAdmin}
          />
        </TabsContent>

        {/*
          API tokens: the first step of the
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
