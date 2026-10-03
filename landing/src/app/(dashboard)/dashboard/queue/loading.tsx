import { Skeleton } from "@/components/ui/skeleton";
import { PageHeaderSkeleton } from "@/design/page-header";

export default function QueueLoading() {
  return (
    <div className="space-y-6">
      <PageHeaderSkeleton titleWidth="w-24" descriptionWidth="w-80" />

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
