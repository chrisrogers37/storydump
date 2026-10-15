### Fixed

- **Catalog refresh and snapshot deletion require explicit owner steps.** Automatic migration application reports them as owed, snapshot deletion requires backup confirmation, and intent constraints are validated after the exclusive-lock transaction commits.
- **Cutover rehearsal requires faithful restoration and zero catalog differences.** Service and owner memberships gate parity for every granted role, and parity summaries identify out-of-scope role reports.
- **Retired migration ledgers have an owner-confirmed conversion path.** Conversion preserves their records, adopts the current chain, and replaces repair advice for colliding migration versions. The cutover runbook covers a Neon branch rehearsal, rollback before the catalog refresh, and required worker start-command settings.
- **Reconciled posts apply posting effects in the verdict transaction.** A publish resolved to posted records its recent-post lock and updates the media item's posting count and time.
