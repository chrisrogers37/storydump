import Link from "next/link";
import { Button } from "@/components/ui/button";
import { DEMO_SIGN_IN_HREF, DEMO_WAITLIST_HREF } from "@/lib/demo/cta";
import { cn } from "@/lib/utils";

/**
 * The sample's one call to action, used by the banner and the end panel:
 * the waitlist, and Sign in as a quiet second for people already invited.
 */
export function DemoCta({ className }: { className?: string }) {
  return (
    <div className={cn("flex flex-wrap items-center gap-x-4 gap-y-2", className)}>
      <Button asChild size="sm">
        <Link href={DEMO_WAITLIST_HREF}>Join the waitlist</Link>
      </Button>
      <Link
        href={DEMO_SIGN_IN_HREF}
        className="text-sm text-muted-foreground underline-offset-4 transition-colors hover:text-foreground hover:underline"
      >
        Already invited? Sign in
      </Link>
    </div>
  );
}
