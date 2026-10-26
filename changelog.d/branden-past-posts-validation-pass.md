### Changed

- **A past post's file that passes the local checks records what it passed.** The asset's `validation_capture -> 'passed'` holds, in a refusal's shape, each check that holds the file to a bound (its size, and a video's duration and frame rate) with the value it measured and the bound the matrix set, beside the byte count, the matrix version and the turn's counters.
- **A refused duration or frame rate records the bounds it was held to, and its sentence says them.** The refusal sentences print the recorded bounds instead of numbers of their own, and a record kept without bounds says its value alone.
- **The checks a file can be refused by are a closed set.** `media_validation.UNSUPPORTED_CHECKS` names them, a refusal outside it fails loudly, and the refusal sentences are tested against the set, which a test holds to exactly the checks the validator names.
