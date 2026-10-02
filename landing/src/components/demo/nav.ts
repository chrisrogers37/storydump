import {
  CalendarDays,
  ImageIcon,
  LayoutDashboard,
  ListChecks,
  Settings,
} from "lucide-react";
import type { SidebarItem } from "@/components/dashboard/sidebar";

/** The sample workspace's home, and the one entry its sidebar matches exactly. */
export const DEMO_HOME = "/demo";

const ELSEWHERE = "In your workspace";

/**
 * The dashboard's navigation, in its order, for the three screens the sample
 * has; Media and Settings stay visible as labels, so the visitor sees the
 * whole product without a link that goes nowhere.
 */
export const DEMO_NAV: SidebarItem[] = [
  { href: DEMO_HOME, label: "Overview", icon: LayoutDashboard },
  { href: `${DEMO_HOME}/queue`, label: "Queue", icon: ListChecks },
  { label: "Media Library", icon: ImageIcon, note: ELSEWHERE },
  { href: `${DEMO_HOME}/calendar`, label: "Calendar", icon: CalendarDays },
  { label: "Settings", icon: Settings, note: ELSEWHERE },
];
