### Changed

- **The archive snapshot migration removes sixteen named cutover copies.** The runner applies the bounded table drops in one transaction and preserves unrelated archive tables.
