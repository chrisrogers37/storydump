"use client";

import { Menu } from "lucide-react";
import { usePathname } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { Sidebar, type SidebarItem } from "@/components/dashboard/sidebar";

/**
 * The navigation drawer: below `lg`, where the sidebar's aside is hidden, a
 * menu button that opens the sidebar's `mobile` variant. The dashboard and the
 * sample workspace draw this one drawer, each with its own entries.
 *
 * KEYED BY THE PATH, so following a link in it closes it. The layout that
 * holds the drawer survives a navigation, and so did its open state: the new
 * page arrived dimmed behind a drawer still open over it (#1649). A new path
 * mounts a new, closed drawer.
 */
export function NavDrawer({ items, home }: { items?: SidebarItem[]; home?: string }) {
  const path = usePathname();

  return (
    <Sheet key={path}>
      <SheetTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="lg:hidden"
          aria-label="Open navigation"
        >
          <Menu className="h-5 w-5" />
        </Button>
      </SheetTrigger>
      {/* A drawer names itself and needs no description; Radix asks
          for an explicit `aria-describedby={undefined}` to say so. */}
      <SheetContent side="left" className="w-64 p-0" aria-describedby={undefined}>
        <SheetTitle className="sr-only">Navigation</SheetTitle>
        {/* `mobile` is not optional here: without it the sidebar takes its
            `hidden … lg:block` variant and the drawer opens onto nothing
            at exactly the widths the drawer exists for (#1363). */}
        <Sidebar mobile items={items} home={home} />
      </SheetContent>
    </Sheet>
  );
}
