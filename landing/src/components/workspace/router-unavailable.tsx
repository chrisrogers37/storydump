import { CloudOff } from "lucide-react";

/**
 * What a screen shows when it could not ask the question.
 *
 * NOT an empty state and never styled as one. "You have no workspaces" and "we
 * could not find out whether you have workspaces" send a reader to opposite
 * actions, and only one of them is something they can act on. An empty state
 * here would be a confident wrong answer — the failure direction nobody checks,
 * because it reads as good news.
 *
 * It is also not an alarm. No warning colour, no error icon, no apology
 * paragraph: this is the expected state of every workspace screen until the
 * target router is mounted, and dressing an expected state as a fault teaches
 * people to ignore the styling that will matter later.
 */
export function RouterUnavailable({ what }: { what: string }) {
  return (
    <div className="flex min-h-[40vh] items-center justify-center">
      <div className="max-w-sm space-y-4 text-center">
        <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-muted">
          <CloudOff className="h-6 w-6 text-muted-foreground" aria-hidden />
        </div>
        <div className="space-y-1.5">
          <h2 className="font-medium">{what} is not available yet.</h2>
          <p className="text-sm text-muted-foreground">
            Nothing is wrong with your account. This part of Storydump is being
            connected — check back shortly.
          </p>
        </div>
      </div>
    </div>
  );
}
