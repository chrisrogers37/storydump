import { Skeleton, TextSkeleton } from "@/components/ui/skeleton";
import { QueueHeader } from "@/components/dashboard/page-headers";

export default function QueueLoading() {
  return (
    <div className="space-y-6">
      <QueueHeader
        tz={
          // The zone is unknown until the config loads. The bar sits between
          // UTC, every workspace's default, and a city zone such as
          // America/New_York, which keeps the line wrapping as the page's
          // does on phones for both; the words are for a screen reader.
          <>
            <TextSkeleton className="w-16" />
            <span className="sr-only">your time zone</span>
          </>
        }
      />

      {/* Queue rows */}
      <div className="divide-y rounded-lg border bg-card">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="flex items-center gap-4 p-4">
            <Skeleton className="h-10 w-10 rounded-md" />
            <div className="flex-1 space-y-2">
              <Skeleton className="h-4 w-48" />
              <Skeleton className="h-3 w-64" />
            </div>
            <Skeleton className="h-5 w-24 rounded-full" />
            <Skeleton className="h-8 w-28 rounded-md" />
          </div>
        ))}
      </div>
    </div>
  );
}
