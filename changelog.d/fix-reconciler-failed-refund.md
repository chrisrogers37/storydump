### Fixed

- **A story the reconciler finds never posted gives its daily slot back.** When Instagram's answer about an uncertain publish says it did not happen (the container expired or errored), the story is marked failed and the slot it took from its day's posting cap is returned in the same transaction, as every other failed publish returns it. Before, the slot stayed spent, so that day could post one story fewer than its cap. A verdict for a story that has already moved on returns nothing.
