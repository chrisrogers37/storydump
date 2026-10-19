### Fixed

- **Schema parity now reports a role membership whose INHERIT or SET option differs.** On PostgreSQL 16 and later a membership edge is compared by its admin, inherit and set options, and an edge holds an option if any of its grants does. A snapshot from a server before 16 has no inherit or set option, so against it an edge compares its admin option alone. Snapshots are now format version 2; an older one recorded memberships without these options and is refused, so extract it again.
