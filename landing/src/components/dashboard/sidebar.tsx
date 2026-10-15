"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  CalendarDays,
  ImageIcon,
  LayoutDashboard,
  ListChecks,
  Settings,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Wordmark } from "@/design/brand";

/**
 * One sidebar entry: a destination, or a label for a screen that is not
 * reachable from here, with a note saying where it is. A label is never a
 * link or a disabled control: a nav item is a promise that a destination
 * exists, and a label makes none.
 */
export type SidebarItem =
  | { label: string; icon: LucideIcon; href: string }
  | { label: string; icon: LucideIcon; note: string };

const navItems: SidebarItem[] = [
  { href: "/dashboard", label: "Overview", icon: LayoutDashboard },
  { href: "/dashboard/queue", label: "Queue", icon: ListChecks },
  { href: "/dashboard/media", label: "Media library", icon: ImageIcon },
  { href: "/dashboard/media/calendar", label: "Calendar", icon: CalendarDays },
  { href: "/dashboard/settings", label: "Settings", icon: Settings },
];

/**
 * No Setup Wizard entry: there is no setup screen for it to point at, and a
 * nav item is a promise that a destination exists. An entry comes with the
 * screen it names.
 *
 * THE SAME RULE, APPLIED AGAIN. `/dashboard/analytics` renders one card
 * reading "Coming soon … planned", so it has no entry here: a destination
 * that exists only to say it does not is the same broken promise with a
 * softer landing.
 *
 * THE PAGE STAYS. Deleting it would turn a URL that answers 200 into a 404
 * for anyone holding the link, which is a behaviour change of its own. It
 * joins this list with the screen it names, or it is deleted separately.
 */
export function Sidebar({
  mobile,
  items = navItems,
  home = "/dashboard",
}: {
  mobile?: boolean;
  /** The entries, in order: the dashboard's own unless a caller has others. */
  items?: SidebarItem[];
  /** Where the name links to, and the one entry matched exactly rather than by prefix. */
  home?: string;
}) {
  const pathname = usePathname();
  const current = activeHref(pathname, items, home);

  return (
    // `lg`, matching the header trigger's `lg:hidden`, because the two are
    // complements: the drawer is the navigation below this width and this
    // aside is the navigation above it. A disagreement between them (`md`
    // here, `lg` there) would show both at 768–1023px. `lg` rather than
    // `md` because at 768px this 224px column leaves the Queue and
    // Calendar under 500px of content; a drawer is the better affordance
    // there (#1363).
    <aside className={mobile ? "w-56 bg-card" : "hidden w-56 shrink-0 border-r bg-card lg:block"}>
      <div className="flex h-14 items-center border-b px-4">
        <Link href={home} className="text-lg">
          <Wordmark />
        </Link>
      </div>
      <nav className="space-y-1 p-3">
        {items.map((item) => {
          if (!("href" in item)) {
            return (
              <div
                key={item.label}
                className="flex items-start gap-3 px-3 py-2 text-sm font-medium text-muted-foreground"
              >
                <item.icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
                <span>
                  {item.label}
                  <span className="block text-xs font-normal">{item.note}</span>
                </span>
              </div>
            );
          }

          const active = item.href === current;

          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={active ? "page" : undefined}
              className={cn(
                "flex items-center gap-3 rounded-full px-3 py-2 text-sm font-medium transition-colors",
                active
                  ? "bg-primary text-primary-foreground"
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

/**
 * The nav item for a path: the longest href it sits under, so the Calendar
 * (/dashboard/media/calendar) is not also the Media library, and home
 * (/dashboard, unless the caller has its own) only on its own page. A label
 * is never the current page.
 */
export function activeHref(
  pathname: string,
  items: SidebarItem[] = navItems,
  home = "/dashboard",
): string | undefined {
  return items
    .flatMap((item) => ("href" in item ? [item.href] : []))
    .filter((href) => pathname === href || (href !== home && pathname.startsWith(href + "/")))
    .sort((a, b) => b.length - a.length)[0];
}
