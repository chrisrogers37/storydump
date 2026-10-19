### Fixed

- **Schema parity now reports a role membership whose INHERIT or SET option differs.** On PostgreSQL 16 and later each membership edge is compared with its admin, inherit and set options, each held if any grant of that edge holds it; before 16 an edge carries its admin option only. Snapshots now carry format version 2, and a snapshot saved in the earlier format, which recorded memberships without those options, is refused rather than compared, so extract it again.
