import { Card, CardContent, CardHeader, StatCard } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

export default function CalendarLoading() {
  return (
    <div className="space-y-6">
      {/* Summary cards */}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
        {Array.from({ length: 3 }).map((_, i) => (
          <StatCard
            key={i}
            label={<Skeleton className="h-4 w-24" />}
            value={<Skeleton className="h-9 w-14" />}
            // Posting Rate always has its interval line.
            detail={i === 2 && <Skeleton className="my-0.5 h-3 w-20" />}
          />
        ))}
      </div>

      {/* Calendar grid */}
      <Card>
        <CardHeader>
          <Skeleton className="h-5 w-32" />
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-7 gap-1">
            {/* Day headers */}
            {Array.from({ length: 7 }).map((_, i) => (
              <Skeleton key={`h-${i}`} className="h-4 w-full" />
            ))}
            {/* Calendar cells */}
            {Array.from({ length: 35 }).map((_, i) => (
              <Skeleton key={i} className="h-20 w-full rounded-md" />
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
