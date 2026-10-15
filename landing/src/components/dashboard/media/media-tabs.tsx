"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";

// Library and Calendar only (#1044): a media screen not on the #1029 spine
// has no tab and no route, rather than one held behind a flag. A tab pointing
// at a missing route is the dangling-nav-link defect (#1032).
const tabs = [
  { href: "/dashboard/media", label: "Library" },
  { href: "/dashboard/media/calendar", label: "Calendar" },
];

export function MediaTabs() {
  const pathname = usePathname();

  return (
    <div className="border-b">
      <nav className="-mb-px flex gap-4" aria-label="Media tabs">
        {tabs.map((tab) => {
          const active = pathname === tab.href;
          return (
            <Link
              key={tab.href}
              href={tab.href}
              className={cn(
                "border-b-2 px-1 py-2 text-sm font-medium transition-colors",
                active
                  ? "border-primary text-primary"
                  : "border-transparent text-muted-foreground hover:border-muted-foreground/30 hover:text-foreground"
              )}
            >
              {tab.label}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
