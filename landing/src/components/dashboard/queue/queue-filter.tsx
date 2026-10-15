import Link from "next/link";
import { PLANNED_QUEUE_HREF, QUEUE_HREF } from "@/lib/intents";
import { cn } from "@/lib/utils";

/**
 * Every story, or only the planned ones (#1413). Two links, not a toggle: the
 * view lives in the address, so it survives a reload and can be shared, and
 * the page filters on the server rather than over the rows it loaded.
 */
export function QueueFilter({ origin }: { origin: "planned" | null }) {
  const views = [
    { href: QUEUE_HREF, label: "All", current: origin === null },
    { href: PLANNED_QUEUE_HREF, label: "Planned", current: origin === "planned" },
  ];

  return (
    <nav aria-label="Queue view" className="flex gap-4 text-sm">
      {views.map((view) => (
        <Link
          key={view.href}
          href={view.href}
          aria-current={view.current ? "page" : undefined}
          className={cn(
            "underline-offset-4 hover:text-foreground",
            view.current ? "font-medium text-foreground underline" : "text-muted-foreground",
          )}
        >
          {view.label}
        </Link>
      ))}
    </nav>
  );
}
