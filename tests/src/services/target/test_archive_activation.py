"""The archive activation loader (#1644).

Two things arm the Instagram archive, and only together: one operator record
that binds THIS release and the mix conversion run, and a whole HMAC key
ring. Every refusal is a named reason, none echoes a value, and `capability` is
the one place the tree builds an enabled `ArchiveCapability` — pinned below by
an AST scan of everything outside `tests/`, so a second construction site fails
CI instead of arming production by accident. No database, no network.

Every record and key here is synthetic.
"""

from __future__ import annotations

import ast
import base64
import dataclasses
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.services.target import archive_activation as aa
from src.services.target.instagram_catalog import TombstoneKeys
from src.services.target.instagram_sources import ArchiveCapability, is_activated
from src.services.target.mix_conversion import MixConversionEvidence
from tests.src.conftest import leaks

ROOT = Path(__file__).resolve().parents[4]

API_SHA, WORKER_SHA, ROLLBACK_SHA = "a" * 40, "b" * 40, "c" * 40
CONVERSION_DIGEST = "d" * 64
RUN_ID = "synthetic-archive-run"
_DEFAULT = object()


def conversion(digest=CONVERSION_DIGEST):
    return MixConversionEvidence(
        "synthetic-conversion-run", digest, API_SHA, WORKER_SHA, (ROLLBACK_SHA,)
    )


def record():
    return {
        "v": 1,
        "enabled": True,
        "run_id": RUN_ID,
        "api": {"sha": API_SHA},
        "worker": {"sha": WORKER_SHA},
        "conversion_run_digest": CONVERSION_DIGEST,
        "evidence": {
            "meta_access": "synthetic:meta-access",
            "graph_version": "synthetic:graph-version",
            "video_validation": "synthetic:video-validation",
            "deployment_drain": "synthetic:deployment-drain",
            "callback_identity": "synthetic:callback-identity",
        },
        "rollback_releases": [{"sha": ROLLBACK_SHA}],
    }


def key_material(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


def b64(raw: bytes, *, padded: bool = False) -> str:
    text = base64.urlsafe_b64encode(raw).decode()
    return text if padded else text.rstrip("=")


K1, K2 = key_material("synthetic-key-1"), key_material("synthetic-key-2")
RING = f"1:{b64(K1)},2:{b64(K2)}"


def write(tmp_path, data, name="activation.json") -> str:
    """*data* is a record (dict) or the file's exact bytes."""
    path = tmp_path / name
    path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
    return str(path)


def env_for(tmp_path, data=None, keys=RING) -> dict:
    env = {aa.RECORD_PATH: write(tmp_path, record() if data is None else data)}
    if keys is not None:
        env[aa.KEYS_VAR] = keys
    return env


def load(path, *, service="worker", release_sha=WORKER_SHA, conv=_DEFAULT):
    return aa.load_evidence(
        path,
        service=service,
        release_sha=release_sha,
        conversion=conversion() if conv is _DEFAULT else conv,
    )


def decide(env, *, service="worker", release_sha=WORKER_SHA, conv=_DEFAULT):
    return aa.resolve(
        env,
        service=service,
        release_sha=release_sha,
        conversion=conversion() if conv is _DEFAULT else conv,
    )


def drop(*path):
    def mutate(data):
        node = data
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]

    return mutate


def put(value, *path):
    def mutate(data):
        node = data
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return mutate


def _evidence_cases():
    for key in aa.EVIDENCE_KEYS:
        yield f"{key} missing", drop("evidence", key)
        yield f"{key} empty", put("", "evidence", key)
        yield f"{key} blank", put(" \t\n ", "evidence", key)
        yield f"{key} one character too long", put("x" * 2049, "evidence", key)
        yield f"{key} not text", put(["synthetic"], "evidence", key)


#: Each way a record fails its own shape, by name. The record is read whole or
#: not at all: no field is optional, and none is half-accepted.
INVALID_RECORDS = {
    "version missing": drop("v"),
    "version two": put(2, "v"),
    "version true": put(True, "v"),
    "version text": put("1", "v"),
    "version float": put(1.0, "v"),
    "disabled": put(False, "enabled"),
    "enabled missing": drop("enabled"),
    "enabled text": put("true", "enabled"),
    "enabled one": put(1, "enabled"),
    "run id missing": drop("run_id"),
    "run id empty": put("", "run_id"),
    "run id with a space": put("run 1", "run_id"),
    "run id with a slash": put("run/1", "run_id"),
    "run id with a newline": put("run\n", "run_id"),
    "run id too long": put("r" * 129, "run_id"),
    "run id not text": put(7, "run_id"),
    "api missing": drop("api"),
    "api not an object": put(API_SHA, "api"),
    "api sha missing": drop("api", "sha"),
    "api sha short": put("a" * 39, "api", "sha"),
    "api sha long": put("a" * 41, "api", "sha"),
    "api sha uppercase": put("A" * 40, "api", "sha"),
    "api sha not hex": put("g" * 40, "api", "sha"),
    "api sha not text": put(["a" * 40], "api", "sha"),
    "worker missing": drop("worker"),
    "worker not an object": put(WORKER_SHA, "worker"),
    "worker sha short": put("b" * 39, "worker", "sha"),
    "worker sha uppercase": put("B" * 40, "worker", "sha"),
    "conversion digest missing": drop("conversion_run_digest"),
    "conversion digest short": put("d" * 63, "conversion_run_digest"),
    "conversion digest long": put("d" * 65, "conversion_run_digest"),
    "conversion digest uppercase": put("D" * 64, "conversion_run_digest"),
    "conversion digest not hex": put("z" * 64, "conversion_run_digest"),
    "conversion digest not text": put(7, "conversion_run_digest"),
    "evidence missing": drop("evidence"),
    "evidence not an object": put(["synthetic"], "evidence"),
    **dict(_evidence_cases()),
    "rollback releases missing": drop("rollback_releases"),
    "rollback releases empty": put([], "rollback_releases"),
    "rollback releases not a list": put({"sha": ROLLBACK_SHA}, "rollback_releases"),
    "rollback release not an object": put([ROLLBACK_SHA], "rollback_releases"),
    "rollback release sha short": put([{"sha": "c" * 39}], "rollback_releases"),
    "one bad rollback release among good": put(
        [{"sha": ROLLBACK_SHA}, {"sha": "nope"}], "rollback_releases"
    ),
}

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


class TestTheRecord:
    def test_the_baseline_record_is_valid(self, tmp_path):
        """The positive control every refusal below is measured against: a
        table of refusals proves nothing if its baseline was never accepted."""
        act = decide(env_for(tmp_path))
        assert act.reason is None and act.evidence is not None
        assert load(write(tmp_path, record())) is not None

    @pytest.mark.parametrize(
        "mutate", INVALID_RECORDS.values(), ids=list(INVALID_RECORDS)
    )
    def test_a_record_failing_any_check_is_invalid(self, tmp_path, mutate):
        data = record()
        mutate(data)
        act = decide(env_for(tmp_path, data))
        assert (act.reason, act.capability, act.evidence) == (
            aa.RECORD_INVALID,
            None,
            None,
        )
        assert load(write(tmp_path, data)) is None

    def test_image_only_activation_is_not_a_record(self, tmp_path):
        """Locked gate: without video validation evidence there
        is no activation, however complete everything else is."""
        data = record()
        del data["evidence"]["video_validation"]
        assert decide(env_for(tmp_path, data)).reason == aa.RECORD_INVALID
        assert aa.EVIDENCE_KEYS == (
            "meta_access",
            "graph_version",
            "video_validation",
            "deployment_drain",
            "callback_identity",
        )

    def test_a_record_without_callback_identity_evidence_is_not_a_record(
        self, tmp_path
    ):
        """Deauthorize and deletion match a callback's `user_id` against the `/me`
        `id` the mapping stores, and code cannot check that they are the same
        id: the operator must attest that Meta's test callback showed it. The
        positive control is the same record, whole, arming."""
        data = record()
        armed = decide(env_for(tmp_path, data))
        assert (armed.reason, armed.capability is not None) == (None, True)
        del data["evidence"]["callback_identity"]
        refused = decide(env_for(tmp_path, data))
        assert (refused.reason, refused.capability, refused.evidence) == (
            aa.RECORD_INVALID,
            None,
            None,
        )
        assert load(write(tmp_path, data)) is None
        # A blank attestation is no attestation.
        data["evidence"]["callback_identity"] = "  "
        assert decide(env_for(tmp_path, data)).reason == aa.RECORD_INVALID

    def test_the_length_boundaries_are_inclusive(self, tmp_path):
        """The positive controls for the length refusals above."""
        data = record()
        data["run_id"] = "r" * 128
        for key in aa.EVIDENCE_KEYS:
            data["evidence"][key] = "x" * 2048
        assert decide(env_for(tmp_path, data)).reason is None

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param(b"", id="empty"),
            pytest.param(b"not json", id="not json"),
            pytest.param(b"{", id="truncated object"),
            pytest.param(b"[]", id="array"),
            pytest.param(b"null", id="null"),
            pytest.param(b'"text"', id="string"),
            pytest.param(b"\xff\xff\xff", id="not utf-8"),
            pytest.param(json.dumps(record()).encode()[:-1], id="truncated record"),
        ],
    )
    def test_a_file_that_is_not_the_record_is_invalid(self, tmp_path, raw):
        assert decide(env_for(tmp_path, raw)).reason == aa.RECORD_INVALID

    def test_the_size_cap_is_exact(self, tmp_path):
        """Mirrors the conversion record's 64 KiB cap. The file is read only
        one byte past it, so a huge file is never pulled into memory."""
        body = json.dumps(record()).encode()
        at_cap = body + b" " * (aa.RECORD_MAX_BYTES - len(body))
        assert aa.RECORD_MAX_BYTES == len(at_cap) == 65536
        assert load(write(tmp_path, at_cap)) is not None
        over = at_cap + b" "
        assert load(write(tmp_path, over)) is None
        assert decide(env_for(tmp_path, over)).reason == aa.RECORD_INVALID

    def test_a_deeply_nested_record_is_invalid_not_a_crash(self, tmp_path):
        nested = b"[" * 30000 + b"]" * 30000
        assert len(nested) < aa.RECORD_MAX_BYTES
        with pytest.raises(RecursionError):  # the input really is adversarial
            json.loads(nested)
        assert decide(env_for(tmp_path, nested)).reason == aa.RECORD_INVALID

    @pytest.mark.parametrize("which", ["missing", "directory", "nul"])
    def test_a_configured_but_unreadable_record_is_invalid_not_absent(
        self, tmp_path, which
    ):
        """Set-but-unreadable is a deploy error an operator must see; only an
        unset or blank variable is the quiet `no_record`."""
        path = {
            "missing": str(tmp_path / "absent.json"),
            "directory": str(tmp_path),
            "nul": "activation\x00.json",
        }[which]
        act = decide({aa.RECORD_PATH: path, aa.KEYS_VAR: RING})
        assert (act.reason, act.capability) == (aa.RECORD_INVALID, None)

    @pytest.mark.parametrize("value", [None, "", "   ", "\n"])
    def test_an_unset_or_blank_path_is_no_record(self, value):
        env = {aa.KEYS_VAR: RING}
        if value is not None:
            env[aa.RECORD_PATH] = value
        act = decide(env)
        assert (act.reason, act.capability) == (aa.NO_RECORD, None)
        assert load(value) is None

    def test_the_evidence_is_ids_and_hashes_only(self, tmp_path):
        path = write(tmp_path, record())
        evidence = load(path)
        assert evidence == aa.ArchiveActivationEvidence(
            RUN_ID,
            hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            API_SHA,
            WORKER_SHA,
            (ROLLBACK_SHA,),
        )
        # The five evidence strings are checked and dropped, never carried.
        assert [f.name for f in dataclasses.fields(evidence)] == [
            "run_id",
            "digest",
            "api_sha",
            "worker_sha",
            "rollback_shas",
        ]

    def test_the_digest_is_the_file_bytes_not_the_parsed_record(self, tmp_path):
        compact = write(
            tmp_path, json.dumps(record(), separators=(",", ":")).encode(), "a.json"
        )
        spaced = write(tmp_path, json.dumps(record(), indent=2).encode(), "b.json")
        assert load(compact).digest != load(spaced).digest
        for path in (compact, spaced):
            assert (
                load(path).digest == hashlib.sha256(Path(path).read_bytes()).hexdigest()
            )


class TestTheBinding:
    def test_each_service_binds_its_own_release(self, tmp_path):
        path = write(tmp_path, record())
        for service, own, other in (
            ("api", API_SHA, WORKER_SHA),
            ("worker", WORKER_SHA, API_SHA),
        ):
            assert load(path, service=service, release_sha=own) is not None
            assert load(path, service=service, release_sha=other) is None
            act = decide(env_for(tmp_path), service=service, release_sha=other)
            assert (act.reason, act.capability) == (aa.RELEASE_MISMATCH, None)

    @pytest.mark.parametrize(
        "release_sha",
        [
            pytest.param(None, id="unset"),
            pytest.param("", id="empty"),
            pytest.param("b" * 39, id="one short"),
            pytest.param("b" * 7, id="abbreviated"),
            pytest.param("b" * 41, id="one long"),
            pytest.param("B" * 40, id="uppercase"),
            pytest.param("c" * 40, id="another release"),
            pytest.param(5, id="not text"),
        ],
    )
    def test_a_release_that_is_not_exactly_the_records_is_a_mismatch(
        self, tmp_path, release_sha
    ):
        act = decide(env_for(tmp_path), release_sha=release_sha)
        assert (act.reason, act.capability) == (aa.RELEASE_MISMATCH, None)

    @pytest.mark.parametrize("service", ["web", "", "API", None])
    def test_an_unknown_service_has_no_release_to_bind(self, tmp_path, service):
        act = decide(env_for(tmp_path), service=service)
        assert (act.reason, act.capability) == (aa.RELEASE_MISMATCH, None)

    @pytest.mark.parametrize(
        "conv",
        [
            pytest.param(None, id="none"),
            pytest.param(SimpleNamespace(digest=CONVERSION_DIGEST), id="lookalike"),
            pytest.param({"digest": CONVERSION_DIGEST}, id="dict"),
            pytest.param(CONVERSION_DIGEST, id="bare digest"),
            pytest.param(True, id="true"),
        ],
    )
    def test_conversion_evidence_must_be_the_real_thing(self, tmp_path, conv):
        act = decide(env_for(tmp_path), conv=conv)
        assert (act.reason, act.capability) == (aa.CONVERSION_MISSING, None)
        assert load(write(tmp_path, record()), conv=conv) is None

    @pytest.mark.parametrize(
        "digest",
        [
            pytest.param("e" * 64, id="another run"),
            pytest.param("d" * 63 + "e", id="last character"),
            pytest.param("e" + "d" * 63, id="first character"),
            pytest.param("D" * 64, id="uppercase"),
            pytest.param("", id="empty"),
        ],
    )
    def test_a_different_conversion_run_is_a_mismatch(self, tmp_path, digest):
        conv = conversion(digest)
        act = decide(env_for(tmp_path), conv=conv)
        assert (act.reason, act.capability) == (aa.CONVERSION_MISMATCH, None)
        assert load(write(tmp_path, record()), conv=conv) is None

    def test_the_matching_conversion_run_is_accepted(self, tmp_path):
        """Positive control for the mismatch table above."""
        assert load(write(tmp_path, record()), conv=conversion(CONVERSION_DIGEST))


class TestTheKeyRing:
    @pytest.mark.parametrize("text", [None, "", "  \n"])
    def test_an_unset_or_blank_ring_is_missing(self, tmp_path, text):
        env = {} if text is None else {aa.KEYS_VAR: text}
        assert aa.load_keys(env) is None
        act = decide(env_for(tmp_path, keys=text))
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
    def test_a_malformed_ring_is_refused_whole(self, tmp_path, text):
        """Never a partial ring: a malformed entry anywhere refuses all of it,
        so a deploy cannot half-rotate onto a key it did not mean to use."""
        assert aa.load_keys({aa.KEYS_VAR: text}) is None
        act = decide(env_for(tmp_path, keys=text))
        assert (act.reason, act.keys, act.capability) == (aa.KEYS_INVALID, None, None)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(7, id="int"),
            pytest.param(b"1:abc", id="bytes"),
            pytest.param(["1:abc"], id="list"),
        ],
    )
    def test_a_ring_that_is_not_text_is_invalid(self, tmp_path, value):
        assert aa.load_keys({aa.KEYS_VAR: value}) is None
        act = decide(env_for(tmp_path, keys=value))
        assert (act.reason, act.keys) == (aa.KEYS_INVALID, None)

    def test_a_ring_repr_carries_no_key_material(self):
        text = repr(aa.load_keys({aa.KEYS_VAR: RING}))
        for secret in (b64(K1), b64(K2), K1.hex(), K2.hex()):
            assert not leaks(text, secret, width=8)


class TestCapability:
    @staticmethod
    def parts(tmp_path):
        evidence = load(write(tmp_path, record()))
        keys = aa.load_keys({aa.KEYS_VAR: RING})
        assert evidence is not None and keys is not None
        return evidence, keys

    def test_evidence_and_a_ring_together_arm(self, tmp_path):
        armed = aa.capability(*self.parts(tmp_path))
        assert armed == ArchiveCapability(enabled=True)
        assert armed.enabled is True and is_activated(armed)

    def test_either_alone_never_arms(self, tmp_path):
        evidence, keys = self.parts(tmp_path)
        assert aa.capability(evidence, None) is None
        assert aa.capability(None, keys) is None
        assert aa.capability(None, None) is None

    def test_lookalikes_never_arm(self, tmp_path):
        evidence, keys = self.parts(tmp_path)
        lookalike_evidence = SimpleNamespace(
            run_id=RUN_ID,
            digest="d" * 64,
            api_sha=API_SHA,
            worker_sha=WORKER_SHA,
            rollback_shas=(ROLLBACK_SHA,),
        )
        # The conversion run's evidence is a different attestation: it must
        # never stand in for the archive's.
        for fake in (
            conversion(),
            lookalike_evidence,
            {"run_id": RUN_ID},
            True,
            RUN_ID,
        ):
            assert aa.capability(fake, keys) is None
        lookalike_keys = SimpleNamespace(current_version=1, keys={1: K1})
        for fake in ({1: K1}, lookalike_keys, True, RING):
            assert aa.capability(evidence, fake) is None


def scenario(tmp_path, reason, base=None):
    """The environment and arguments that make `decide` refuse for *reason*,
    every other input being valid. A reason added to the vocabulary without a
    scenario here fails loudly rather than going untested."""
    data = record() if base is None else base
    env, kwargs = env_for(tmp_path, data), {}
    if reason == aa.NO_RECORD:
        del env[aa.RECORD_PATH]
    elif reason == aa.RECORD_INVALID:
        env = env_for(tmp_path, {**data, "enabled": False})
    elif reason == aa.RELEASE_MISMATCH:
        kwargs["release_sha"] = "f" * 40
    elif reason == aa.CONVERSION_MISSING:
        kwargs["conv"] = None
    elif reason == aa.CONVERSION_MISMATCH:
        kwargs["conv"] = conversion("e" * 64)
    elif reason == aa.KEYS_MISSING:
        del env[aa.KEYS_VAR]
    elif reason == aa.KEYS_INVALID:
        env[aa.KEYS_VAR] = f"1:{b64(K1)},2:short"
    else:
        raise AssertionError(f"no scenario for the new reason {reason!r}")
    return env, kwargs


class TestResolve:
    def test_the_vocabulary_is_the_designs_in_check_order(self):
        assert aa.REASONS == (
            "no_record",
            "record_invalid",
            "release_mismatch",
            "conversion_missing",
            "conversion_mismatch",
            "keys_missing",
            "keys_invalid",
        )

    def test_a_valid_record_and_ring_arm(self, tmp_path):
        env = env_for(tmp_path)
        act = decide(env)
        digest = hashlib.sha256(Path(env[aa.RECORD_PATH]).read_bytes()).hexdigest()
        assert act.capability == ArchiveCapability(enabled=True)
        assert act.reason is None
        assert act.evidence == aa.ArchiveActivationEvidence(
            RUN_ID, digest, API_SHA, WORKER_SHA, (ROLLBACK_SHA,)
        )
        assert act.keys.current_version == 2
        assert act.log_line == "instagram archive: armed (run=synthetic-archive-run)"

    def test_the_api_arms_from_its_own_release(self, tmp_path):
        act = decide(env_for(tmp_path), service="api", release_sha=API_SHA)
        assert act.capability == ArchiveCapability(enabled=True)
        assert act.evidence.run_id == RUN_ID

    def test_nothing_set_is_inactive_by_default(self):
        act = decide({})
        assert (act.capability, act.evidence, act.keys) == (None, None, None)
        assert act.log_line == "instagram archive: inactive (no_record)"

    @pytest.mark.parametrize("reason", aa.REASONS)
    def test_each_refusal_is_named_in_the_log_line(self, tmp_path, reason):
        env, kwargs = scenario(tmp_path, reason)
        act = decide(env, **kwargs)
        assert (act.capability, act.evidence, act.reason) == (None, None, reason)
        assert act.log_line == f"instagram archive: inactive ({reason})"

    def test_an_evidence_reason_is_named_before_a_key_reason(self, tmp_path):
        assert decide({aa.KEYS_VAR: "junk"}).reason == aa.NO_RECORD
        garbage = {aa.RECORD_PATH: write(tmp_path, b"{"), aa.KEYS_VAR: "junk"}
        assert decide(garbage).reason == aa.RECORD_INVALID
        no_ring = env_for(tmp_path, keys=None)
        assert decide(no_ring, release_sha="f" * 40).reason == aa.RELEASE_MISMATCH
        junk_ring = env_for(tmp_path, keys="junk")
        assert decide(junk_ring, conv=None).reason == aa.CONVERSION_MISSING
        wrong_run = conversion("e" * 64)
        assert decide(junk_ring, conv=wrong_run).reason == aa.CONVERSION_MISMATCH

    def test_the_evidence_checks_run_in_vocabulary_order(self, tmp_path):
        env = env_for(tmp_path)
        wrong = {"release_sha": "f" * 40, "conv": None}
        assert decide(env, **wrong).reason == aa.RELEASE_MISMATCH
        disabled = env_for(tmp_path, {**record(), "enabled": False})
        assert decide(disabled, **wrong).reason == aa.RECORD_INVALID

    def test_the_ring_loads_on_its_own_for_receipts(self, tmp_path):
        env = env_for(tmp_path)
        del env[aa.RECORD_PATH]
        act = decide(env)
        assert (act.reason, act.capability, act.evidence) == (aa.NO_RECORD, None, None)
        assert act.keys.current_version == 2
        assert dict(act.keys.keys) == {1: K1, 2: K2}

    def test_evidence_is_withheld_unless_the_archive_is_armed(self, tmp_path):
        env = env_for(tmp_path, keys=None)
        act = decide(env)
        assert (act.reason, act.evidence, act.keys, act.capability) == (
            aa.KEYS_MISSING,
            None,
            None,
            None,
        )
        # The record on its own is still readable by the loader.
        assert load(env[aa.RECORD_PATH]) is not None

    @pytest.mark.parametrize(
        "path", [5, b"/x", ["a"], object(), None], ids=lambda v: type(v).__name__
    )
    @pytest.mark.parametrize(
        "keys", [5, b"1:x", ["1:x"], object(), None], ids=lambda v: type(v).__name__
    )
    def test_garbage_in_the_environment_never_raises(self, path, keys):
        act = decide({aa.RECORD_PATH: path, aa.KEYS_VAR: keys})
        assert act.capability is None and act.reason in aa.REASONS


class TestNothingIsEchoed:
    """The record's evidence strings may carry private references, and the key
    ring is a secret: neither reaches a result's repr, the log line, a log
    record or a stream, armed or refused."""

    PRIVATE = {
        "meta_access": "zq-private-ref-meta-8f3a91c2",
        "graph_version": "zq-private-ref-graph-5d70be14",
        "video_validation": "zq-private-ref-video-c41f08a7",
        "deployment_drain": "zq-private-ref-drain-92be6d35",
        "callback_identity": "zq-private-ref-callback-3be17a60",
    }

    @pytest.mark.parametrize("reason", [None, *aa.REASONS], ids=["armed", *aa.REASONS])
    def test_no_value_reaches_a_result_a_log_or_a_stream(
        self, tmp_path, caplog, capsys, reason
    ):
        base = {**record(), "evidence": dict(self.PRIVATE)}
        if reason is None:
            env, kwargs = env_for(tmp_path, base), {}
        else:
            env, kwargs = scenario(tmp_path, reason, base)
        with caplog.at_level(logging.DEBUG):
            act = decide(env, **kwargs)
            evidence = load(env.get(aa.RECORD_PATH), **kwargs)
            keys = aa.load_keys(env)
        assert (reason is None) == (act.capability is not None)
        out, err = capsys.readouterr()
        surfaces = "\n".join(
            [
                repr(act),
                str(act),
                act.log_line,
                repr(evidence),
                repr(keys),
                caplog.text,
                out,
                err,
            ]
        )
        for value in self.PRIVATE.values():
            assert value not in surfaces
        for secret in (b64(K1), b64(K2), K1.hex(), K2.hex()):
            assert not leaks(surfaces, secret, width=8)
        if env.get(aa.RECORD_PATH):
            assert env[aa.RECORD_PATH] not in surfaces


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
