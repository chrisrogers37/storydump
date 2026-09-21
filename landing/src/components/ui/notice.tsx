import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * The banner this app says things in, because it has no toast.
 *
 * SIXTEEN HAND-WRITTEN COPIES (#1341), and the copies disagreed about the
 * thing that matters least visually and most to a screen reader: four of
 * the nine error boxes carried `role="alert"` and five did not, so the same
 * kind of failure was announced on some screens and silent on others. Two
 * green boxes used `text-green-900` where five used `text-green-800`.
 *
 * ROLE FOLLOWS TONE, not the author's memory. An error interrupts —
 * `role="alert"` is an assertive live region, which is right for "that did
 * not save". A success or an informational note is `role="status"`, a
 * polite one, which is right for "saved" and wrong for an interruption.
 * `setup/callout.tsx` is the marketing side's equivalent; this is the
 * dashboard's, and they stay separate because the marketing one carries
 * icons and a dark-mode palette that these banners never had.
 */
export type NoticeTone = "error" | "success" | "info";

export const NOTICE_CLASS: Record<NoticeTone, string> = {
  error: "rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800",
  success:
    "rounded-md border border-green-200 bg-green-50 p-3 text-sm text-green-800",
  info: "rounded-md border bg-muted/40 p-3 text-sm",
};

/** Assertive for a failure, polite for everything else. */
export function noticeRole(tone: NoticeTone): "alert" | "status" {
  return tone === "error" ? "alert" : "status";
}

export function Notice({
  tone = "info",
  onDismiss,
  className,
  children,
}: {
  tone?: NoticeTone;
  /** Renders a Dismiss control. Only the Accounts tab has ever had one. */
  onDismiss?: () => void;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      role={noticeRole(tone)}
      className={cn(
        NOTICE_CLASS[tone],
        // The one dismissible banner lays its row out this way; the classes and
        // the label are the Accounts tab's own, carried over rather than
        // redesigned, so the only thing that changes there is the role.
        onDismiss && "flex items-center justify-between",
        className,
      )}
    >
      {onDismiss ? (
        <>
          <span>{children}</span>
          <button
            onClick={onDismiss}
            className="ml-2 text-red-600 hover:text-red-800 font-medium"
          >
            Dismiss
          </button>
        </>
      ) : (
        children
      )}
    </div>
  );
}
