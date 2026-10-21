"""The archive activation (#1644): on wherever the key ring is.

The Instagram archive needs no operator record and no switch. A process arms it
whenever the `ARCHIVE_HMAC_KEYS` ring loads whole, because that ring keys the
tombstones and deletion receipts the archive cannot run without. A missing or
malformed ring is a named reason, and then nothing arms. An armed process keys
its reconciliation jobs to its own release's run
(`mix_conversion.release_run`), so each deploy retries what an earlier one could
not finish. `capability` is the one place the tree builds an enabled
`ArchiveCapability`, pinned below by an AST scan of everything outside `tests/`,
so a second construction site fails CI. No database, no network.

Every key here is synthetic.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.services.target import archive_activation as aa
from src.services.target import mix_conversion
from src.services.target.instagram_catalog import TombstoneKeys
from src.services.target.instagram_sources import ArchiveCapability, is_activated
from tests.src.conftest import leaks

ROOT = Path(__file__).resolve().parents[4]

API_SHA, WORKER_SHA = "a" * 40, "b" * 40


def key_material(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


def b64(raw: bytes, *, padded: bool = False) -> str:
    text = base64.urlsafe_b64encode(raw).decode()
    return text if padded else text.rstrip("=")


K1, K2 = key_material("synthetic-key-1"), key_material("synthetic-key-2")
RING = f"1:{b64(K1)},2:{b64(K2)}"


def ring_env(keys=RING) -> dict:
    return {} if keys is None else {aa.KEYS_VAR: keys}


def decide(env, *, release_sha=WORKER_SHA):
    return aa.resolve(env, release_sha=release_sha)


#: Each way a key ring is malformed, by name. One bad entry refuses the ring.
BAD_RINGS = {
    "no colon": b64(K1),
    "empty version": f":{b64(K1)}",
    "version zero": f"0:{b64(K1)}",
    "version negative": f"-1:{b64(K1)}",
    "version leading zero": f"01:{b64(K1)}",
    "version signed": f"+1:{b64(K1)}",
    "version not a number": f"a:{b64(K1)}",
    "version past the column": f"1000000000:{b64(K1)}",
    # U+0661 ARABIC-INDIC DIGIT ONE: `str.isdigit()` and `\d` accept it, and
    # `int()` reads it as 1; the grammar must not.
    "version in another script": f"{chr(0x661)}:{b64(K1)}",
    "version with a digit from another script": f"1{chr(0x661)}:{b64(K1)}",
    "empty key": "1:",
    "key one byte under the floor": f"1:{b64(K1[:31])}",
    "key with a plus": "1:+" + b64(K1)[1:],
    "key with a slash": "1:/" + b64(K1)[1:],
    "key with trailing junk": f"1:{b64(K1)}!",
    "key with an inner space": f"1:{b64(K1)[:10]} {b64(K1)[10:]}",
    "key with too much padding": f"1:{b64(K1)}==",
    "key with three padding characters": f"1:{b64(K1)}===",
    "key with padding it does not need": f"1:{b64(bytes(33))}==",
    "key of a length no encoder makes": "1:" + "A" * 45,
    "duplicate version": f"1:{b64(K1)},1:{b64(K2)}",
    "trailing comma": f"1:{b64(K1)},",
    "empty entry": f"1:{b64(K1)},,2:{b64(K2)}",
    "second key too short": f"1:{b64(K1)},2:short",
    "third entry malformed": f"1:{b64(K1)},2:{b64(K2)},3:!!",
    "entries split by a newline": f"1:{b64(K1)}\n2:{b64(K2)}",
}


class TestTheKeyRing:
    @pytest.mark.parametrize("text", [None, "", "  \n"])
    def test_an_unset_or_blank_ring_is_missing(self, text):
        assert aa.load_keys(ring_env(text)) is None
        act = decide(ring_env(text))
        assert (act.reason, act.keys, act.capability) == (aa.KEYS_MISSING, None, None)

    def test_one_key_is_a_ring(self):
        keys = aa.load_keys({aa.KEYS_VAR: f"1:{b64(K1)}"})
        assert isinstance(keys, TombstoneKeys)
        assert keys.current_version == 1 and dict(keys.keys) == {1: K1}

    def test_the_highest_version_is_current_in_any_order(self):
        for text in (RING, f"2:{b64(K2)},1:{b64(K1)}"):
            keys = aa.load_keys({aa.KEYS_VAR: text})
            assert keys.current_version == 2
            assert dict(keys.keys) == {1: K1, 2: K2}

    def test_the_highest_version_is_numeric_not_textual(self):
        keys = aa.load_keys({aa.KEYS_VAR: f"9:{b64(K1)},10:{b64(K2)}"})
        assert keys.current_version == 10
        assert dict(keys.keys) == {9: K1, 10: K2}

    def test_padded_and_unpadded_spellings_are_the_same_key(self):
        assert b64(K1, padded=True) != b64(K1)
        for text in (b64(K1), b64(K1, padded=True)):
            keys = aa.load_keys({aa.KEYS_VAR: f"1:{text}"})
            assert dict(keys.keys) == {1: K1}

    def test_whitespace_around_entries_is_tolerated(self):
        keys = aa.load_keys({aa.KEYS_VAR: f" 1:{b64(K1)} ,\t2:{b64(K2)}\n"})
        assert dict(keys.keys) == {1: K1, 2: K2}

    def test_the_key_floor_is_thirty_two_bytes(self):
        """Positive controls for the 31-byte refusal in the table below."""
        for size in (32, 33, 64):
            raw = bytes(range(size))
            keys = aa.load_keys({aa.KEYS_VAR: f"1:{b64(raw)}"})
            assert dict(keys.keys) == {1: raw}

    def test_the_largest_version_that_fits_the_column_is_accepted(self):
        keys = aa.load_keys({aa.KEYS_VAR: f"999999999:{b64(K1)}"})
        assert keys.current_version == 999999999

    @pytest.mark.parametrize("text", BAD_RINGS.values(), ids=list(BAD_RINGS))
    def test_a_malformed_ring_is_refused_whole(self, text):
        """Never a partial ring: a malformed entry anywhere refuses all of it,
        so a deploy cannot half-rotate onto a key it did not mean to use."""
        assert aa.load_keys({aa.KEYS_VAR: text}) is None
        act = decide(ring_env(text))
        assert (act.reason, act.keys, act.capability) == (aa.KEYS_INVALID, None, None)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(7, id="int"),
            pytest.param(b"1:abc", id="bytes"),
            pytest.param(["1:abc"], id="list"),
        ],
    )
    def test_a_ring_that_is_not_text_is_invalid(self, value):
        assert aa.load_keys({aa.KEYS_VAR: value}) is None
        act = decide(ring_env(value))
        assert (act.reason, act.keys) == (aa.KEYS_INVALID, None)

    def test_a_ring_repr_carries_no_key_material(self):
        text = repr(aa.load_keys({aa.KEYS_VAR: RING}))
        for secret in (b64(K1), b64(K2), K1.hex(), K2.hex()):
            assert not leaks(text, secret, width=8)


class TestTheReleaseRun:
    def test_the_run_is_the_release_s_and_fits_the_job_functions(self):
        """`fn_archive_reconcile_jobs` and `fn_mix_conversion_jobs` mint nothing
        for a run id or digest outside these shapes."""
        evidence = aa.release_evidence(WORKER_SHA)
        assert evidence.run_id == "release-" + WORKER_SHA[:12]
        assert re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", evidence.run_id)
        assert re.fullmatch(r"[0-9a-f]{64}", evidence.digest)
        assert (evidence.run_id, evidence.digest) == mix_conversion.release_run(
            WORKER_SHA
        )

    def test_one_release_is_one_run_and_another_release_another(self):
        assert aa.release_evidence(WORKER_SHA) == aa.release_evidence(WORKER_SHA)
        mine, next_one = aa.release_evidence(WORKER_SHA), aa.release_evidence(API_SHA)
        assert mine.run_id != next_one.run_id and mine.digest != next_one.digest

    def test_the_api_and_the_worker_of_one_release_share_its_run(self):
        assert decide(ring_env()).evidence == decide(ring_env()).evidence
        assert mix_conversion.release_evidence(WORKER_SHA).digest == (
            aa.release_evidence(WORKER_SHA).digest
        )

    @pytest.mark.parametrize(
        "release_sha",
        [None, "", "  ", "b" * 39, "g" * 40, "b" * 41, 7],
        ids=["unset", "empty", "blank", "short", "not hex", "long", "not text"],
    )
    def test_a_process_with_no_usable_release_runs_as_local(self, release_sha):
        evidence = aa.release_evidence(release_sha)
        assert evidence.run_id == "release-local"
        assert evidence.digest == aa.release_evidence(None).digest

    def test_a_release_is_read_as_railway_writes_it(self):
        spelled = aa.release_evidence(f" {WORKER_SHA.upper()}\n")
        assert spelled == aa.release_evidence(WORKER_SHA)


class TestCapability:
    @staticmethod
    def parts():
        evidence = aa.release_evidence(WORKER_SHA)
        keys = aa.load_keys({aa.KEYS_VAR: RING})
        assert keys is not None
        return evidence, keys

    def test_a_run_and_a_ring_together_arm(self):
        armed = aa.capability(*self.parts())
        assert armed == ArchiveCapability(enabled=True)
        assert armed.enabled is True and is_activated(armed)

    def test_either_alone_never_arms(self):
        evidence, keys = self.parts()
        assert aa.capability(evidence, None) is None
        assert aa.capability(None, keys) is None
        assert aa.capability(None, None) is None

    def test_lookalikes_never_arm(self):
        evidence, keys = self.parts()
        lookalike_evidence = SimpleNamespace(
            run_id=evidence.run_id, digest=evidence.digest
        )
        # The conversion's run is another type: it never stands in for the
        # archive's, even for the same release.
        for fake in (
            mix_conversion.release_evidence(WORKER_SHA),
            lookalike_evidence,
            {"run_id": evidence.run_id},
            True,
            evidence.run_id,
        ):
            assert aa.capability(fake, keys) is None
        lookalike_keys = SimpleNamespace(current_version=1, keys={1: K1})
        for fake in ({1: K1}, lookalike_keys, True, RING):
            assert aa.capability(evidence, fake) is None


class TestResolve:
    def test_the_vocabulary_is_the_ring_s_alone(self):
        assert aa.REASONS == ("keys_missing", "keys_invalid")

    def test_a_ring_arms_with_nothing_else_set(self):
        act = decide(ring_env())
        assert act.capability == ArchiveCapability(enabled=True)
        assert act.reason is None
        assert act.evidence == aa.release_evidence(WORKER_SHA)
        assert act.keys.current_version == 2
        assert act.log_line == "instagram archive: armed (run=release-bbbbbbbbbbbb)"

    def test_the_retired_record_variables_change_nothing(self, tmp_path):
        """The operator records are gone: a leftover variable, even one naming
        a file that is not a record, neither arms nor disarms anything."""
        junk = tmp_path / "leftover.json"
        junk.write_text("{")
        env = {
            **ring_env(),
            "INSTAGRAM_ARCHIVE_ACTIVATION_RECORD": str(junk),
            "SOURCE_MIX_CONVERSION_RECORD": str(junk),
        }
        assert decide(env) == decide(ring_env())
        no_ring = {k: v for k, v in env.items() if k != aa.KEYS_VAR}
        assert decide(no_ring).reason == aa.KEYS_MISSING

    def test_nothing_set_is_inactive(self):
        act = decide({})
        assert (act.capability, act.evidence, act.keys) == (None, None, None)
        assert act.log_line == "instagram archive: inactive (keys_missing)"

    @pytest.mark.parametrize(
        "reason, keys",
        [(aa.KEYS_MISSING, None), (aa.KEYS_INVALID, f"1:{b64(K1)},2:short")],
    )
    def test_each_refusal_is_named_in_the_log_line(self, reason, keys):
        act = decide(ring_env(keys))
        assert (act.capability, act.evidence, act.reason) == (None, None, reason)
        assert act.log_line == f"instagram archive: inactive ({reason})"

    def test_every_reason_has_a_refusal_above(self):
        assert set(aa.REASONS) == {aa.KEYS_MISSING, aa.KEYS_INVALID}

    @pytest.mark.parametrize(
        "keys", [5, b"1:x", ["1:x"], object(), None], ids=lambda v: type(v).__name__
    )
    @pytest.mark.parametrize(
        "release_sha", [5, b"x", None, "f" * 40], ids=lambda v: type(v).__name__
    )
    def test_garbage_in_the_environment_never_raises(self, keys, release_sha):
        act = decide({aa.KEYS_VAR: keys}, release_sha=release_sha)
        assert act.capability is None and act.reason in aa.REASONS


class TestNothingIsEchoed:
    """The key ring is a secret: it reaches no result's repr, the log line, a
    log record or a stream, armed or refused."""

    @pytest.mark.parametrize("keys", [RING, None, f"1:{b64(K1)},2:short"])
    def test_no_key_reaches_a_result_a_log_or_a_stream(self, caplog, capsys, keys):
        with caplog.at_level(logging.DEBUG):
            act = decide(ring_env(keys))
            loaded = aa.load_keys(ring_env(keys))
        out, err = capsys.readouterr()
        surfaces = "\n".join(
            [repr(act), str(act), act.log_line, repr(loaded), caplog.text, out, err]
        )
        for secret in (b64(K1), b64(K2), K1.hex(), K2.hex()):
            assert not leaks(surfaces, secret, width=8)


# --- the one construction site ----------------------------------------------------

ALLOWED = ROOT / "src" / "services" / "target" / "archive_activation.py"
CODE_ROOTS = ("src", "scripts", "storydump_cli")


def _is_capability(node, names) -> bool:
    return (isinstance(node, ast.Name) and node.id in names) or (
        isinstance(node, ast.Attribute) and node.attr == "ArchiveCapability"
    )


def _provably_disabled(call: ast.Call) -> bool:
    """`ArchiveCapability()`, or `enabled` (by keyword or position) given only
    the literal False: anything else could be an enabled capability."""
    values = list(call.args)
    for keyword in call.keywords:
        if keyword.arg != "enabled":  # another name, or **options
            return False
        values.append(keyword.value)
    return all(isinstance(v, ast.Constant) and v.value is False for v in values)


def armed_sites(source: str) -> list[int]:
    """The lines where *source* builds, or could build, an enabled capability:
    a call that is not provably disabled, through any import alias, assignment
    alias or attribute path, and a subclass (which could default `enabled` on).

    A KNOWN LIMIT, stated rather than hidden: the rule is syntactic. A class
    fetched with `getattr`, or an instance flipped with `object.__setattr__`,
    is invisible to it; the dataclass is frozen, and review is the rest."""
    tree = ast.parse(source)
    names = {"ArchiveCapability"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {
                a.asname
                for a in node.names
                if a.name == "ArchiveCapability" and a.asname
            }
    grew = True
    while grew:
        grew = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _is_capability(node.value, names):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in names:
                        names.add(target.id)
                        grew = True
    sites = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and _is_capability(node.func, names)
            and not _provably_disabled(node)
        ):
            sites.append(node.lineno)
        elif isinstance(node, ast.ClassDef) and any(
            _is_capability(base, names) for base in node.bases
        ):
            sites.append(node.lineno)
    return sorted(sites)


def _code_files() -> list[Path]:
    return sorted(
        [p for top in CODE_ROOTS for p in (ROOT / top).rglob("*.py")]
        + list(ROOT.glob("*.py"))
    )


class TestOnlyThisModuleArms:
    @pytest.mark.parametrize(
        "source",
        [
            pytest.param("ArchiveCapability(enabled=True)", id="keyword true"),
            pytest.param("ArchiveCapability(True)", id="positional true"),
            pytest.param("ArchiveCapability(enabled=flag)", id="a variable"),
            pytest.param("ArchiveCapability(**options)", id="unpacked keywords"),
            pytest.param("ArchiveCapability(*flags)", id="unpacked positionals"),
            pytest.param("ArchiveCapability(enabled=not False)", id="an expression"),
            pytest.param(
                "from x import ArchiveCapability as Cap\nCap(enabled=True)",
                id="an import alias",
            ),
            pytest.param(
                "Cap = ArchiveCapability\nCap(enabled=True)", id="an assignment alias"
            ),
            pytest.param(
                "Cap = Other = ArchiveCapability\nOther(True)", id="a chained alias"
            ),
            pytest.param("sources.ArchiveCapability(enabled=True)", id="an attribute"),
            pytest.param(
                "class Armed(ArchiveCapability):\n    enabled: bool = True",
                id="a subclass",
            ),
            pytest.param(
                "class Armed(sources.ArchiveCapability):\n    pass",
                id="a subclass by attribute",
            ),
        ],
    )
    def test_the_scan_sees_every_way_to_build_an_armed_capability(self, source):
        assert armed_sites(source) != []

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param("ArchiveCapability()", id="the default"),
            pytest.param("ArchiveCapability(enabled=False)", id="keyword false"),
            pytest.param("ArchiveCapability(False)", id="positional false"),
            pytest.param("def f(c: ArchiveCapability | None): ...", id="annotation"),
            pytest.param("isinstance(c, ArchiveCapability)", id="isinstance"),
            pytest.param("set_enabled(enabled=True)", id="another call"),
            pytest.param("OtherCapability(enabled=True)", id="another class"),
            pytest.param("# ArchiveCapability(enabled=True)", id="a comment"),
            pytest.param('"""ArchiveCapability(enabled=True)"""', id="a docstring"),
        ],
    )
    def test_the_scan_leaves_what_cannot_arm_alone(self, source):
        assert armed_sites(source) == []

    def test_no_module_but_archive_activation_builds_an_armed_capability(self):
        """Inactive by default: the one construction site is reachable
        only through evidence AND keys. Tests may build their own."""
        files = _code_files()
        # The scan has teeth: it walked the tree and found the class's home.
        assert len(files) > 100
        assert ALLOWED in files
        assert ROOT / "src" / "services" / "target" / "instagram_sources.py" in files
        offenders = {
            str(path.relative_to(ROOT)): sites
            for path in files
            if path != ALLOWED
            and (sites := armed_sites(path.read_text(encoding="utf-8")))
        }
        assert offenders == {}

    def test_the_one_construction_is_capabilitys_own(self):
        source = ALLOWED.read_text(encoding="utf-8")
        sites = armed_sites(source)
        assert len(sites) == 1  # the scan sees it
        owners = [
            node.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef)
            and node.lineno <= sites[0] <= node.end_lineno
        ]
        assert owners == ["capability"]
