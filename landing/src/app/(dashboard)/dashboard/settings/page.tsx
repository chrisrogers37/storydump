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
 * Settings, read-only — a WAYPOINT, not a finished screen (#1063).
 *
 * It used to ask for `init`, a route that does not exist and is not planned, so
 * the hard bail below fired on EVERY load and this screen rendered
 * `RouterUnavailable` every time — taking Accounts and Integrations with it.
 * "Router unavailable" also misdiagnosed: the router was fine; this page was
 * asking it for something it never served.
 *
 * ── Why the screen is read-only rather than repointed ──────────────────────
 *
 * All six Settings writes target routes that do not exist (`schedule`,
 * `toggle-setting`, `switch-account`, `remove-account`, `disconnect-gdrive`,
 * `sync-media`). Both connect flows targeted `oauth-url/<provider>` and are
 * now DELETED rather than gated: they were per-workspace against a
 * per-source route, so they were never going to be wired as written, and
 * behind `editable` they would have come back the moment the writes landed.
 * Repointing the reads alone is the one shape #1051 explicitly refused: "the
 * form would show real current values beside a save button that silently 404s,
 * and someone would change a setting to match what they saw."
 *
 * So the values are real and the controls are inert and SAY they are inert.
 * Nothing here is undone by wiring the writes: `editable` becomes true,
 * `Unavailable` fields gain sources. It is a strict subset of that work —
 * and `editable` now gates ONE kind of thing, controls that are pending,
 * so flipping it cannot resurrect anything that was removed as invalid.
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

      <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
        <p className="font-medium">These settings are read-only for now.</p>
        <p className="mt-1">
          The values below are your workspace&apos;s current settings. Changing
          them is not wired up yet, so every control is disabled rather than
          shown as something you can save — a control that looks editable and
          silently fails is worse than one that says it is not. Some fields have
          no source on this API yet and are marked unavailable rather than shown
          as a default.
        </p>
      </div>

      <Tabs defaultValue="general">
        <TabsList>
          <TabsTrigger value="general">General</TabsTrigger>
          <TabsTrigger value="accounts">Accounts</TabsTrigger>
          <TabsTrigger value="integrations">Integrations</TabsTrigger>
        </TabsList>

        <TabsContent value="general">
          <GeneralTab settings={settings} workspaceId={workspaceId} editable={false} />
        </TabsContent>

        <TabsContent value="accounts">
          <AccountsTab accounts={accounts} editable={false} />
        </TabsContent>

        <TabsContent value="integrations">
          <IntegrationsTab settings={settings} editable={false} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
