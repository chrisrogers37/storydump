import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

export default function SetupLoading() {
  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div>
        <Skeleton className="h-8 w-20" />
        <Skeleton className="h-4 w-72 mt-2" />
      </div>

      {/* Progress bar */}
      <div className="flex items-center gap-2">
        {Array.from({ length: 7 }).map((_, i) => (
          <Skeleton key={i} className="h-8 w-8 rounded-full" />
        ))}
      </div>

      {/* Wizard step card */}
      <Card>
        <CardContent className="pt-6 space-y-4">
          <Skeleton className="h-6 w-48" />
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-3/4" />
          <Skeleton className="h-10 w-40 mt-4" />
        </CardContent>
      </Card>
    </div>
  );
}
