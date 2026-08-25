"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";

// Dead Content and Content Reuse are GONE, not hidden (#1044). Both were
// legacy analytics features that are not in the `01` design and not on the
// #1029 spine, so they are not in the target product yet; their screens are
// deleted rather than held behind a flag. A tab pointing at a deleted route is
// the dangling-nav-link defect #1032 already had to fix once.
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
