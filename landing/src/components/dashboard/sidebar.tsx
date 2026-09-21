"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  CalendarDays,
  ImageIcon,
  LayoutDashboard,
  ListChecks,
  Settings,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { siteConfig } from "@/config/site";

const navItems = [
  { href: "/dashboard", label: "Overview", icon: LayoutDashboard },
  { href: "/dashboard/queue", label: "Queue", icon: ListChecks },
  { href: "/dashboard/media", label: "Media Library", icon: ImageIcon },
  { href: "/dashboard/media/calendar", label: "Calendar", icon: CalendarDays },
  { href: "/dashboard/settings", label: "Settings", icon: Settings },
];

/**
 * The Setup Wizard entry is GONE, along with the flag that used to hide it.
 *
 * `/dashboard/setup` was deleted in this change; its replacement,
 * `/dashboard/connections`, is not built yet. So the entry pointed at nothing —
 * and it was shown BY DEFAULT to exactly the users this change creates, because
 * `showSetupWizard` was computed as "onboarding not complete", which is true of
 * every brand-new workspace by construction. First signup, first click, 404.
 *
 * Deleted rather than pointed somewhere placeholder: a nav item is a promise
 * that a destination exists, and there is no destination. It comes back with
 * the screen it names.
 *
 * THE SAME RULE, APPLIED AGAIN (TD-D4). `/dashboard/analytics` was in this
 * list and its destination renders one card reading "Coming Soon … planned
 * for Phase 3". A nav item is a promise that a destination exists; a
 * destination that exists only to say it does not is the same broken promise
 * with a softer landing. The entry is gone.
 *
 * THE PAGE IS NOT. Deleting it would turn a URL that answers 200 today into
 * a 404 for anyone holding the link, which is a behaviour change this
 * cleanup does not get to make. It comes back to this list with the screen
 * it names, or it is deleted under its own ruling.
 */
export function Sidebar({ mobile }: { mobile?: boolean }) {
  const pathname = usePathname();
  const visibleItems = navItems;

  return (
    <aside className={mobile ? "w-56 bg-card" : "hidden w-56 shrink-0 border-r bg-card md:block"}>
      <div className="flex h-14 items-center border-b px-4">
        <Link href="/dashboard" className="text-lg font-semibold tracking-tight">
          {siteConfig.name}
        </Link>
      </div>
      <nav className="space-y-1 p-3">
        {visibleItems.map((item) => {
          const active =
            item.href === "/dashboard"
              ? pathname === "/dashboard"
              : pathname === item.href || pathname.startsWith(item.href + "/");

          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                active
                  ? "bg-primary/10 text-primary"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground"
              )}
            >
              <item.icon className="h-4 w-4" />
              {item.label}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
