### Changed

- **The activation nudge's daily cap is a setting, and three when unset (#1653).** `TARGET_ACTIVATION_NUDGE_LIMIT` sets how many emails one armed day may queue; the default was 20. A person is marked nudged as their email is queued, not as it is sent, so a provider mistake uses up that day's nudges, and the first armed run now risks three. A value that is not a whole number of at least 1 stops the worker at boot, naming the variable. The nudge itself stays off.

### Tests

- **The nudge's switch, bounds and overlap are pinned by behaviour (#1653).** The worker's config now comes from one function the tests call, so the switch and the cap are checked where `main` reads them. The three bounds of a live run, the sweep entry that passes them, and the arming log line have tests. A gate test has an overlapping sweep latch a person between this sweep's read and its UPDATE, so what keeps the second email from going is the UPDATE's predicate, not its wording.

### Documentation

- **The plan says what the nudge's code does (#1653).** Section 49 names all three gates (the switch, an email provider and the web origin), says the first card counts as activity at every stage, and records the cap. Migration 106's own header, which names two gates, is applied and stays as it is.
