import { PageHeader } from "@/design/page-header";
import { TextSkeleton } from "@/components/ui/skeleton";

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

/** `tz` is unknown until the config loads; the placeholder passes nothing and
 *  gets a bar about as wide as a typical zone name in its place. */
export function QueueHeader({ tz }: { tz?: string }) {
  return (
    <PageHeader
      title="Queue"
      description={
        <>
          Every post that is not done yet, in slot order. Times are in{" "}
          {tz ?? <TextSkeleton className="w-28" />}.
        </>
      }
    />
  );
}
