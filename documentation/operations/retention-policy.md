# Retention policy

Periodic retention is deliberately limited to rate counters. The database
function `fn_retention_batch` is the enforcement boundary: it accepts only
`rate_counters` and raises an error for every other class. The scheduled
`scheduler.execute_retention_sweep` calls that function for
`rate_counters`; the default keep period is seven days.

| Data class | Periodic retention | Operator guidance |
| --- | --- | --- |
| `rate_counters` | periodic retention on | The retention sweep deletes rows older than its configured keep period in bounded batches. The default is seven days. |
| `audit_events` | periodic retention off | Audit history accumulates; include it in storage and backup capacity planning. |
| `jobs` | periodic retention off | Job history accumulates; include it in storage and backup capacity planning. |
| `provider_operations` | periodic retention off | Provider-operation history accumulates; include it in storage and backup capacity planning. |
| `channel_outbox` | periodic retention off | Outbox rows accumulate; include them in storage and backup capacity planning. |
| `daily_post_counts` | periodic retention off | The daily cap ledger accumulates; include it in storage and backup capacity planning. |

“Periodic retention off” means the scheduled retention sweep does not apply a
TTL or delete these rows based on age. It does not describe workspace or user
deletion workflows, which are separate targeted operations. This document
does not establish an additional deletion schedule or make legal-retention
promises.

When storage needs change, operators should use the established deployment and
database procedures and account for the retention state above when planning
capacity and backups. Do not add a class to the periodic sweep without updating
the database allowlist and this policy together.
