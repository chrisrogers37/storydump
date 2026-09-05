import { redirect } from "next/navigation";
import { getSession } from "@/lib/session";
import { workspaceFetch } from "@/lib/workspaces";
import {
  deriveSettings,
  type AccountsResponse,
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
import { IntegrationsTab } from "@/components/dashboard/settings/integrations-tab";

/**
 * Settings — General writes, Accounts writes ONE thing (adding a destination,
 * #1089), Integrations does not yet (#1057/#1063).
 *
 * It used to ask for `init`, a route that does not exist and is not planned, so
 * the hard bail below fired on EVERY load and this screen rendered
 * `RouterUnavailable` every time — taking Accounts and Integrations with it.
 * "Router unavailable" also misdiagnosed: the router was fine; this page was
 * asking it for something it never served.
 *
 * ── `editable` is per TAB, because readiness is per tab ────────────────────
 *
 * All six Settings writes used to target routes that do not exist. Four of
 * them — the `settings_change` controls on General — are now on the command
 * client (epic P3), so that tab is editable. The other two tabs are NOT — and
 * note `editable` is not the same question as "can this tab write": Accounts
 * now carries a working destination form (#1089) that is deliberately outside
 * the flag, because the flag marks controls whose ROUTE does not exist yet.
 * This is one flag passed three times rather than one screen-wide state:
 * `switch-account` has no target-tier home at all, `remove-account` and
 * `disconnect-gdrive` map to `disconnect_account` which is UNBUILT, and
 * `sync-media` is the epic's P4. Flipping those with General would be exactly
 * the shape #1051 refused — "a save button that silently 404s".
 *
 * Both connect flows targeted `oauth-url/<provider>` and were DELETED rather
 * than gated (#1070): per-workspace against a per-source route, never wirable
 * as written, and behind `editable` they would have come back the moment this
 * change landed. That deletion is what makes flipping the flag here safe;
 * `editable` gates ONE kind of thing, controls that are pending and coming
 * back, so it cannot resurrect anything removed as invalid.
 *
 * ── There is no page-level read-only banner, deliberately ──────────────────
 *
 * There was one, and it made a single claim about three tabs. That was true
 * while all three were read-only and became false the moment one was not. The
 * two remaining read-only tabs each carry their own notice at the control it
 * is about (`accounts-tab.tsx`, `integrations-tab.tsx`), which is where a
 * reader meets it; a page-level restatement could only be a coarser copy of
 * those, and after this change a WRONG one. If a third tab ever becomes
 * editable, nothing here needs editing — which is the point.
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
  searchParams: Promise<{ connected?: string }>;
}) {
  // `auth.py:364` has always redirected here with `?connected=gdrive` on a
  // successful Drive grant. This page took NO searchParams at all, so the
  // parameter was unreadable by construction and the grant completed in
  // silence — #1090 B3's other half.
  const { connected } = await searchParams;
  const session = await getSession().catch(() => null);
  if (!session) redirect("/login");
  const workspaceId = session.activeWorkspaceId;
  if (!workspaceId) redirect("/welcome");

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

  const [configResult, accountsResult, sourcesResult, bindingsResult, membersResult, statsResult] =
    await Promise.all([
      workspaceFetch<WorkspaceConfig>("", workspaceId),
      workspaceFetch<AccountsResponse>("accounts", workspaceId),
      workspaceFetch<SourcesResponse>("sources", workspaceId),
      workspaceFetch<BindingsResponse>("bindings", workspaceId),
      workspaceFetch<MembersResponse>("members", workspaceId),
      workspaceFetch<StatsResponse>("stats", workspaceId),
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

  const settings = deriveSettings(
    configResult.data,
    sourcesResult.data.sources ?? [],
    statsResult.data,
  );
  const accounts = accountsResult.data.accounts ?? [];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Your posting schedule, accounts, and integrations.
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
        <div
          role="status"
          className="rounded-md border border-green-200 bg-green-50 p-3 text-sm text-green-900"
        >
          <span className="font-medium">Instagram account connected.</span>{" "}
          Storydump will keep its access fresh. Publishing through Instagram
          directly is not switched on yet; approvals still post by hand.
        </div>
      )}

      {connected === "gdrive" && (
        <div
          role="status"
          className="rounded-md border border-green-200 bg-green-50 p-3 text-sm text-green-900"
        >
          <span className="font-medium">Google Drive access was granted.</span>{" "}
          The grant completed, so an empty library is not a failed connection.
          Its current state is shown on the source under Integrations.
        </div>
      )}

      <Tabs defaultValue="general">
        <TabsList>
          <TabsTrigger value="general">General</TabsTrigger>
          <TabsTrigger value="accounts">Accounts</TabsTrigger>
          <TabsTrigger value="integrations">Integrations</TabsTrigger>
        </TabsList>

        <TabsContent value="general">
          <GeneralTab
            settings={settings}
            workspaceId={workspaceId}
            workspaceName={workspaceName}
            editable
            workspaceState={configResult.data.state}
            restorableUntil={configResult.data.restorable_until}
            isOwner={isOwner}
            members={
              <MembersCard
                workspaceId={workspaceId}
                members={membersResult.ok ? (membersResult.data.members ?? []) : null}
                currentUserId={session.userId}
                canRemove={membership?.role === "owner" || membership?.role === "admin"}
              />
            }
          />
        </TabsContent>

        {/*
          NOT flipped with General: `editable` now gates only `switch-account`,
          which has no target-tier home yet (epic P6) — Connect and Remove are
          real and ungated inside the tab (Remove = `disable_account`,
          owner decision 2026-09-04).
        */}
        <TabsContent value="accounts">
          <AccountsTab
            accounts={accounts}
            editable={false}
            workspaceId={workspaceId}
          />
        </TabsContent>

        {/*
          `editable` stays false HERE too, and it now gates less than it used
          to: `sync-media` is P4 and `disconnect-gdrive` is P6, so those two
          stay disabled. The per-source Connect button is NOT behind it — it
          calls a route that exists (#1065), so gating it would be the
          reads-without-writes harm inverted, hiding a control that works.
        */}
        <TabsContent value="integrations">
          <IntegrationsTab
            settings={settings}
            sources={sourcesResult.data.sources ?? []}
            bindings={bindingsResult.ok ? (bindingsResult.data.bindings ?? []) : null}
            workspaceId={workspaceId}
            telegramLinked={session.telegramLinked}
            telegramDisplayName={session.telegramDisplayName}
          />
        </TabsContent>
      </Tabs>
    </div>
  );
}
