import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui/notice";
import { RESOLVED_IN } from "@/lib/conditions";
import {
  SUPERSEDED_COPY,
  connectionOutcomeCopy,
  isSuperseded,
  type ConnectionResult,
} from "@/lib/connection-result";

/**
 * The Settings banner for what an Instagram connect applied (#1644,
 * `connection-result.ts`). A polite status, like every success here.
 *
 * Every outcome links to the current source status, the Posting mix card; the
 * one that asks the person to act (`connected_history_off_mix_conflict`, whose
 * past posts were left Off) makes that link its action, Review posting mix.
 * A plain link, not `next/link`: Settings opens its tab from `?tab=` once, on
 * mount, so a client-side navigation from Accounts would leave Accounts open;
 * a document load opens General. The browser's own fragment scroll does not
 * reach the card on that load (the dashboard scrolls inside `<main>`, not the
 * document), so Settings scrolls it into view itself once it has mounted
 * (`ScrollToHash`, `lib/fragment-scroll.ts`).
 */
export function ConnectionResultBanner({ result }: { result: ConnectionResult }) {
  const review = result.outcome === "connected_history_off_mix_conflict";
  return (
    <Notice tone="success">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="min-w-0 break-words">
          <span className="font-medium">{connectionOutcomeCopy(result)}</span>
          {isSuperseded(result) && <> {SUPERSEDED_COPY}</>}
        </p>
        {review ? (
          <Button size="sm" asChild>
            <a href={RESOLVED_IN.mix.href}>Review posting mix</a>
          </Button>
        ) : (
          <a href={RESOLVED_IN.mix.href} className="font-medium underline underline-offset-2">
            {RESOLVED_IN.mix.action}
          </a>
        )}
      </div>
    </Notice>
  );
}
