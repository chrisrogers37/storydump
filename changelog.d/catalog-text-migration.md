### Changed

- **Catalog refresh uses the schema-code function definitions and removes unused intent fields.** The migration refuses populated legacy fields under a table lock, limits retention to rate counters, and records adoption only when its structural conditions hold.
