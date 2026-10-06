import { PageHeader } from "@/design/page-header";

/* Each header is shared by its page and that page's loading.tsx, so the
 * placeholder's line under the title wraps exactly as the page's does. The
 * Overview keeps PageHeaderSkeleton: dashboard/loading.tsx also stands in for
 * other routes, so it must not show the Overview's words, and its one short
 * line doesn't wrap. */

export function SettingsHeader() {
  return (
    <PageHeader
      title="Settings"
      description="Your posting schedule, accounts, integrations, and API tokens."
    />
  );
}

/** `tz` is the zone's name, or the placeholder's stand-in for it. */
export function QueueHeader({ tz }: { tz: React.ReactNode }) {
  return (
    <PageHeader
      title="Queue"
      description={
        <>
          Every post that is not done yet, in slot order. Times are in{" "}
          {tz}.
        </>
      }
    />
  );
}
