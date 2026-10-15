import { notFound } from "next/navigation";
import { AdmitAddress, WaitlistAction } from "@/components/ops/waitlist-actions";
import { Card } from "@/components/ui/card";
import { Notice } from "@/components/ui/notice";
import { PageHeader } from "@/design/page-header";
import {
  joinedLabel,
  STATE_LABELS,
  WAITLIST_COPY,
  waitingLine,
  waitlistRows,
} from "@/lib/ops-waitlist";
import { getSessionToken } from "@/lib/session";
import { targetFetch } from "@/lib/target-api";

export const metadata = { title: "Waitlist" };

/** The API's refusals that make this page the site's 404. */
const REFUSED = new Set([401, 403, 404]);

/**
 * The waitlist and every admission made off it, newest first (#1723), each
 * with where it stands: waiting → Admit; admitted or emailed → Send again;
 * signed up → nothing. Above it, a field admits any address.
 *
 * ── Its own refusal is what holds ──────────────────────────────────────────
 *
 * Next renders the layout and this page in parallel, so the layout's 404 for
 * someone who is not an operator does not stop this read. The read is the
 * API's to refuse (`require_ops`), and its 401, 403 or 404 is this page's
 * 404, the same one the layout gives.
 *
 * A read that fails otherwise says so. It is never shown as an empty list,
 * which would tell the owner that no one is waiting.
 */
export default async function OpsWaitlistPage() {
  const token = await getSessionToken();
  if (!token) notFound();
  const result = await targetFetch("/ops/waitlist", token);
  if (!result.ok && REFUSED.has(result.status)) notFound();
  const rows = result.ok ? waitlistRows(result.data) : null;

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <PageHeader title={WAITLIST_COPY.title} description={WAITLIST_COPY.description} />

      <Card className="p-4">
        <AdmitAddress />
      </Card>

      {rows === null ? (
        <Notice tone="error">{WAITLIST_COPY.unreadable}</Notice>
      ) : rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">{WAITLIST_COPY.empty}</p>
      ) : (
        <section aria-labelledby="waitlist-rows" className="space-y-2">
          <h2 id="waitlist-rows" className="text-sm font-semibold">
            {waitingLine(rows)}
          </h2>
          <div className="overflow-x-auto rounded-xl border bg-card">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b text-left text-xs text-muted-foreground">
                  <th scope="col" className="px-4 py-2 font-medium">
                    Address
                  </th>
                  <th scope="col" className="hidden px-4 py-2 font-medium sm:table-cell">
                    Joined (UTC)
                  </th>
                  <th scope="col" className="hidden px-4 py-2 font-medium sm:table-cell">
                    State
                  </th>
                  <th scope="col" className="px-4 py-2">
                    <span className="sr-only">Action</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const stateLabel = STATE_LABELS[row.state];
                  const joined = joinedLabel(row);
                  return (
                    <tr key={row.email} className="border-b align-top last:border-b-0">
                      <td className="px-4 py-3">
                        <span className="font-medium [overflow-wrap:anywhere]">{row.email}</span>
                        {/* A phone has room for the address and its action: the
                            two columns beside them fold into one line under it. */}
                        <span className="mt-0.5 block text-xs text-muted-foreground sm:hidden">
                          {stateLabel} · {joined}
                        </span>
                      </td>
                      <td className="hidden px-4 py-3 whitespace-nowrap text-muted-foreground sm:table-cell">
                        {joined}
                      </td>
                      <td className="hidden px-4 py-3 whitespace-nowrap sm:table-cell">
                        {stateLabel}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <WaitlistAction email={row.email} state={row.state} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}
