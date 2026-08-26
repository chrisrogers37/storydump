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
import { IntegrationsTab } from "@/components/dashboard/settings/integrations-tab";

/**
 * Settings — General writes; Accounts and Integrations do not yet (#1057/#1063).
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
 * client (epic P3), so that tab is editable. The other two tabs are NOT, and
 * this is one flag passed three times rather than one screen-wide state:
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
export default async function SettingsPage() {
  const session = await getSession().catch(() => null);
  if (!session) redirect("/login");
  const workspaceId = session.activeWorkspaceId;
  if (!workspaceId) redirect("/welcome");

  const [configResult, accountsResult, sourcesResult, statsResult] =
    await Promise.all([
      workspaceFetch<WorkspaceConfig>("", workspaceId),
      workspaceFetch<AccountsResponse>("accounts", workspaceId),
      workspaceFetch<SourcesResponse>("sources", workspaceId),
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

      <Tabs defaultValue="general">
        <TabsList>
          <TabsTrigger value="general">General</TabsTrigger>
          <TabsTrigger value="accounts">Accounts</TabsTrigger>
          <TabsTrigger value="integrations">Integrations</TabsTrigger>
        </TabsList>

        <TabsContent value="general">
          <GeneralTab settings={settings} workspaceId={workspaceId} editable />
        </TabsContent>

        {/*
          NOT flipped with General, and the reason is per-tab. `switch-account`
          has no target-tier home at all and `remove-account` maps to
          `disconnect_account`, which is UNBUILT — so these controls stay
          disabled-with-reason per F5 (b) until the epic's P6.
        */}
        <TabsContent value="accounts">
          <AccountsTab accounts={accounts} editable={false} />
        </TabsContent>

        {/* Likewise: `sync-media` is P4 and `disconnect-gdrive` is P6. */}
        <TabsContent value="integrations">
          <IntegrationsTab settings={settings} editable={false} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
