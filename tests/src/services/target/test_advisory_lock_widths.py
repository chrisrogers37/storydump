"""Two advisory-lock hash widths, held apart on purpose (#1370, TD-B19).

`identity.py` hashes with `hashtext` (32-bit); `provisioning.py` and
`category_mix.py` with `hashtextextended` (64-bit). That is not two spellings
of one thing — it was ruled in #1370 after measuring, and this file is the
ratchet that makes changing either a decision rather than a tidy-up.

THE THREE FACTS THE RULING RESTS ON
-----------------------------------

1. A collision can only OVER-serialize. The key is what the lock is on, so two
   colliding keys share a lock and queue; nothing is ever let through that
   should have been held. Both widths are correct — the wide one only buys
   less false contention.

2. The rate is not close. At 32 bits the chance of ANY collision passes 1% at
   ~9,884 distinct keys. Production held 3 `user_identities` rows when this was
   measured (2026-09-22). `provisioning.py` chose the wide variant because
   folder keys are dense "at estate scale"; subjects are not.

3. **The namespaces are disjoint**, which is what makes two widths safe at all.
   `identity:` is hashed only at 32 bits; `case_mix:`, `sources:` and
   `media_source:` only at 64. No key string is hashed both ways, so no two
   callers can take "the same" lock through different functions and fail to
   exclude each other. THIS is the property that would break if someone
   half-unified them, and it is the one asserted below.

WHY UNIFYING IS A DEPLOY PLAN, NOT A PATCH
------------------------------------------

Changing the function changes every key's value. During a rolling deploy old
and new processes compute different keys for the same logical resource, so for
the length of the rollout the lock does not hold and two workers can enter a
section meant to be serialized. Doing it needs a quiesce-and-switch or a
dual-take release. If you are here because you want to unify them, that is the
cost to plan for — not this test to delete.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[4] / "src" / "services" / "target"

#: Key-prefix → the hash function that namespace is locked with.
EXPECTED_WIDTH = {
    "identity:": "hashtext",
    "case_mix:": "hashtextextended",
    "sources:": "hashtextextended",
    "media_source:": "hashtextextended",
}

#: `(module, hash function, key expression)` for every hashed advisory lock.
_LOCK = re.compile(
    r"pg_advisory_xact_lock\((hashtext|hashtextextended)\("
    r"(?::(\w+)|.*?)\)?[^)]*\)\"\s*\)\s*,\s*\{\s*\"\w+\":\s*f?\"([^\"]*)\"",
    re.S,
)


def _sites():
    found = []
    for module in sorted(_SRC.glob("*.py")):
        text = module.read_text()
        for m in _LOCK.finditer(text):
            found.append((module.name, m.group(1), m.group(3)))
    return found


class TestTheTwoWidthsStayApart:
    def test_every_hashed_lock_site_is_found(self):
        """Positive control: the regex sees the sites, so a pass below means
        agreement rather than an empty scan."""
        sites = _sites()
        assert len(sites) >= 5, f"expected every hashed lock site, saw {sites}"
        assert {s[0] for s in sites} >= {
            "identity.py",
            "provisioning.py",
            "category_mix.py",
        }

    def test_each_namespace_is_hashed_at_one_width_only(self):
        """The property that makes two widths safe.

        A namespace hashed both ways would mean two callers taking "the same"
        lock through different functions — and silently not excluding one
        another. That is the failure mode, not the existence of two widths.
        """
        by_prefix: dict[str, set[str]] = {}
        for _module, func, key in _sites():
            prefix = key.split("{", 1)[0]
            by_prefix.setdefault(prefix, set()).add(func)
        for prefix, funcs in by_prefix.items():
            assert len(funcs) == 1, (
                f"namespace {prefix!r} is hashed {sorted(funcs)} — two callers"
                " of one lock through different functions do not exclude"
                " each other (#1370)"
            )

    def test_the_widths_are_the_ones_that_were_ruled(self):
        """Changing one is a deploy plan (module docstring), so it has to be
        said here first."""
        for _module, func, key in _sites():
            prefix = key.split("{", 1)[0]
            assert prefix in EXPECTED_WIDTH, (
                f"new advisory-lock namespace {prefix!r}: say which width it"
                " takes and why, here and at the call site (#1370)"
            )
            assert func == EXPECTED_WIDTH[prefix], (
                f"{prefix!r} is hashed with {func}, ruled {EXPECTED_WIDTH[prefix]}"
                " — changing it changes every key's value, so a rolling deploy"
                " leaves the lock not holding for the length of the rollout"
            )
