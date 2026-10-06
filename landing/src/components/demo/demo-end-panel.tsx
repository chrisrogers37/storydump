"use client";

import { X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DemoCta } from "@/components/demo/demo-cta";
import { useDemo } from "@/components/demo/demo-provider";
import { endPanelDue } from "@/lib/demo/state";

/**
 * The sample's close, once the visitor has decided three stories or opened
 * all three pages.
 *
 * STICKY AT THE FOOT OF THE MAIN COLUMN, the last thing in it. While the page
 * scrolls it rides the bottom of the view, so it is on screen the moment it
 * appears without moving anything above it; at the end of the page it takes
 * its own place in the flow, so the last row and its buttons are never
 * trapped underneath. Non-modal: it takes no focus and can be closed.
 */
export function DemoEndPanel() {
  const { state, dismiss } = useDemo();
  if (!endPanelDue(state)) return null;

  return (
    <div className="sticky bottom-0 mt-6 flex justify-end">
      <section
        role="status"
        aria-label="Sample workspace"
        className="relative w-full rounded-lg border bg-card p-4 pr-12 shadow-lg sm:max-w-sm"
      >
        <p className="font-medium">That&apos;s the job: one tap per Story.</p>
        <p className="mt-1 text-sm text-muted-foreground">
          Want this for your own Drive folder?
        </p>
        <DemoCta className="mt-4" />
        <Button
          variant="ghost"
          size="icon"
          aria-label="Close"
          className="absolute right-2 top-2"
          onClick={dismiss}
        >
          <X className="h-4 w-4" />
        </Button>
      </section>
    </div>
  );
}
