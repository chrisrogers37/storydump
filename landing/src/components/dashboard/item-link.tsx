import { Link2 } from "lucide-react";
import { httpsHref } from "@/lib/redirect-guard";
import { cn } from "@/lib/utils";

/**
 * An item's link to add by hand, on its Media Library card and on each of its
 * Queue rows (#1413 phase 7). The link is what a person typed, so it is shown as
 * text, and it becomes an anchor only when `httpsHref` gives it an href.
 *
 * It reaches no network and holds no state: the Queue's rows draw it, and the
 * sample workspace draws the same rows.
 */
export function ItemLink({ link, className }: { link: string; className?: string }) {
  const href = httpsHref(link);
  return (
    <p
      className={cn(
        "flex min-w-0 items-center gap-1 text-xs text-muted-foreground",
        className,
      )}
    >
      <Link2 className="h-3 w-3 shrink-0" aria-hidden />
      <span className="sr-only">Link to add by hand: </span>
      {href ? (
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          title={link}
          className="truncate underline-offset-2 hover:underline"
        >
          {link}
        </a>
      ) : (
        <span className="truncate" title={link}>
          {link}
        </span>
      )}
    </p>
  );
}
