"""Persisted policy protocol and the posting TTL fallback."""

import importlib

import pytest


def policy():
    spec = importlib.util.find_spec("src.services.target.source_policy")
    assert spec is not None, "central policy service is missing"
    return importlib.import_module("src.services.target.source_policy")


def request(**changes):
    return dict(
        schema_version=1,
        expected_revision=1,
        minimum_age_days=90,
        include_credited=False,
        include_unknown=False,
        **changes,
    )


def test_defaults_and_projection_are_the_canonical_saved_effective_contract():
    p = policy()
    saved = p.policy_defaults()
    assert saved == dict(
        schema_version=2,
        revision=1,
        minimum_age_days=90,
        include_credited=False,
        include_unknown=False,
        carousel_slides="all",
        eligibility="supported",
        ranking="none",
        destination_scope="origin_only",
        new_connection_use_history=True,
    )
    assert p.project_policy(saved, 120) == {
        "schema_version": 2,
        "revision": 1,
        "policy": {
            k: saved[k]
            for k in p.ROW_DEFAULTS
            if k not in ("schema_version", "revision")
        },
        "effective": {"minimum_age_days": 121},
        "bounds": {"minimum_age_days": {"min": 0, "max": 36500}},
        "choices": {"carousel_slides": ["all", "first"]},
        "capabilities": {
            "editable": [
                "minimum_age_days",
                "include_credited",
                "include_unknown",
                "carousel_slides",
            ]
        },
    }
    assert p.policy_defaults() == saved


def test_the_history_default_is_part_of_the_one_definition_and_has_no_row():
    """One versioned default definition: the switch a new connection is
    offered starts where this says, and nothing else says it. It is no column of
    a source's policy row, so it is the one default that is read, never saved."""
    p = policy()
    defaults = p.policy_defaults()
    assert defaults["new_connection_use_history"] is True
    assert set(defaults) - set(p.ROW_DEFAULTS) == {"new_connection_use_history"}


async def test_a_new_source_materializes_the_rows_columns_and_nothing_more(
    monkeypatch,
):
    p = policy()
    bound = []

    class Executor:
        async def execute(self, statement, params=None):
            bound.append(params)

    monkeypatch.setattr(p.source_state, "require_source_lock", lambda *args: None)

    await p.materialize_policy(Executor(), workspace_id="ws", source_id="sid")

    assert set(bound[0]) == {"ws", "sid", *p.ROW_DEFAULTS}
    assert bound[0]["minimum_age_days"] == 90 and bound[0]["revision"] == 1


@pytest.mark.parametrize("value", [True, False, 90.0, -1, 36501, "90", None])
def test_age_requires_actual_bounded_integer(value):
    p = policy()
    body = request()
    body["minimum_age_days"] = value
    with pytest.raises(p.PolicyRefused) as error:
        p.validate_update(body)
    assert error.value.reason == "invalid_source_policy"


@pytest.mark.parametrize("field", ["include_credited", "include_unknown"])
@pytest.mark.parametrize("value", [0, 1, "false", None])
def test_optins_require_independent_booleans(field, value):
    p = policy()
    body = request()
    body[field] = value
    with pytest.raises(p.PolicyRefused) as error:
        p.validate_update(body)
    assert error.value.reason == "invalid_source_policy"


@pytest.mark.parametrize(
    "extra",
    [
        "account_id",
        "credential_id",
        "eligibility",
        "ranking",
        "destination_scope",
        "future",
    ],
)
def test_unknown_and_uneditable_keys_are_unsupported(extra):
    p = policy()
    body = request()
    body[extra] = "future"
    with pytest.raises(p.PolicyRefused) as error:
        p.validate_update(body)
    assert error.value.reason == "unsupported_source_policy"


@pytest.mark.parametrize("version", [3, True, "1", None])
def test_schema_versions_are_closed(version):
    p = policy()
    body = request()
    body["schema_version"] = version
    with pytest.raises(p.PolicyRefused) as error:
        p.validate_update(body)
    assert error.value.reason == "unsupported_source_policy"


@pytest.mark.parametrize("field", list(request()))
def test_every_request_field_is_required(field):
    p = policy()
    body = request()
    del body[field]
    with pytest.raises(p.PolicyRefused) as error:
        p.validate_update(body)
    assert error.value.reason == "invalid_source_policy"


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 3),
        ("eligibility", "ranked"),
        ("ranking", "quarter"),
        ("destination_scope", "explicit"),
    ],
)
def test_future_persisted_modes_are_refused(field, value):
    p = policy()
    saved = p.policy_defaults()
    saved[field] = value
    with pytest.raises(p.PolicyRefused) as error:
        p.project_policy(saved, None)
    assert error.value.reason == "unsupported_source_policy"


@pytest.mark.parametrize(
    "raw,resolved", [(None, 30), (0, 30), (1, 1), (120, 120), (-2, -2)]
)
def test_posting_and_policy_share_exact_ttl_resolution(raw, resolved):
    from src.services.target import intent_ledger

    assert hasattr(intent_ledger, "resolve_repost_ttl_days"), (
        "shared posting TTL resolver is missing"
    )
    assert intent_ledger.resolve_repost_ttl_days(raw) == resolved
    p = policy()
    saved = p.policy_defaults()
    saved["minimum_age_days"] = 0
    view = p.project_policy(saved, raw)
    assert view["policy"]["minimum_age_days"] == 0
    assert view["effective"]["minimum_age_days"] == max(0, resolved + 1)


def test_valid_boundary_values_and_optins_are_preserved():
    p = policy()
    for age in (0, 36500):
        body = request()
        body.update(minimum_age_days=age, include_credited=True, include_unknown=False)
        assert p.validate_update(body) == body


def test_version_two_requires_and_preserves_the_slide_choice():
    p = policy()
    body = request()
    body.update(schema_version=2, carousel_slides="first")
    assert p.validate_update(body) == body
    del body["carousel_slides"]
    with pytest.raises(p.PolicyRefused, match="invalid_source_policy"):
        p.validate_update(body)


@pytest.mark.parametrize("value", [None, True, 1, "middle", ["all"]])
def test_slide_choices_are_closed(value):
    p = policy()
    body = request()
    body.update(schema_version=2, carousel_slides=value)
    with pytest.raises(p.PolicyRefused, match="unsupported_source_policy"):
        p.validate_update(body)


def test_version_one_remains_three_fields_and_get_advertises_two():
    p = policy()
    body = request()
    assert p.validate_update(body) == body
    body["carousel_slides"] = "all"
    with pytest.raises(p.PolicyRefused, match="unsupported_source_policy"):
        p.validate_update(body)
    saved = p.policy_defaults()
    saved.update(schema_version=1, carousel_slides="first")
    view = p.project_policy(saved, 30)
    assert view["schema_version"] == 2
    assert view["policy"]["carousel_slides"] == "first"
    assert view["choices"]["carousel_slides"] == ["all", "first"]
