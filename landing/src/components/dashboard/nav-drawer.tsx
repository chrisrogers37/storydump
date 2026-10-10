"use client";

import { useState, type MouseEvent } from "react";
import { Menu } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { Sidebar, type SidebarItem } from "@/components/dashboard/sidebar";

/** The sidebar's entries: the dashboard's own unless a caller has others. */
type Entries = { items?: SidebarItem[]; home?: string };

/**
 * The navigation drawer: below `lg`, where the sidebar's aside is hidden, a
 * menu button that opens the sidebar's `mobile` variant. The dashboard and the
 * sample workspace draw this one drawer, each with its own entries.
 *
 * IT CLOSES WHEN A LINK IN IT IS FOLLOWED (#1649). The layout that holds the
 * drawer survives a navigation, and so did its open state: the new page
 * arrived dimmed behind a drawer still open over it.
 */
export function NavDrawer(entries: Entries) {
  const [open, setOpen] = useState(false);
  return <NavDrawerView {...entries} open={open} onOpenChange={setOpen} />;
}

/** Whether a click landed on a link, or on something inside one. */
function followsLink(event: MouseEvent<HTMLElement>): boolean {
  return (event.target as Element).closest("a[href]") !== null;
}

/** The drawer in a given state: everything but the state, so it reads as a tree. */
export function NavDrawerView({
  items,
  home,
  open,
  onOpenChange,
}: Entries & { open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
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
      <SheetContent
        side="left"
        className="w-64 p-0"
        aria-describedby={undefined}
        onClick={(event) => {
          if (followsLink(event)) onOpenChange(false);
        }}
      >
        <SheetTitle className="sr-only">Navigation</SheetTitle>
        {/* `mobile` is not optional here: without it the sidebar takes its
            `hidden … lg:block` variant and the drawer opens onto nothing
            at exactly the widths the drawer exists for (#1363). */}
        <Sidebar mobile items={items} home={home} />
      </SheetContent>
    </Sheet>
  );
}
