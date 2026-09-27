"""Tests for revocable capability grants.

These guard the property the whole module exists for: that a permission you
take back actually stops working. A grant system that only *records* revocations
is worse than none, because it teaches you that revoking is safe.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from security.grants import (
    Capability,
    DenyReason,
    Grant,
    GrantStore,
)


@pytest.fixture()
def store() -> Iterator[GrantStore]:
    with GrantStore(":memory:") as grants:
        yield grants


# -- nothing is granted by default -----------------------------------------


@pytest.mark.parametrize("capability", list(Capability))
def test_no_capability_is_granted_by_default(
    store: GrantStore, capability: Capability
) -> None:
    decision = store.check(capability)
    assert decision.allowed is False
    assert decision.reason is DenyReason.NOT_GRANTED


def test_a_fresh_store_reports_nothing_granted(store: GrantStore) -> None:
    assert store.active() == []
    assert store.granted() == frozenset()


# -- granting and using -----------------------------------------------------


def test_grant_allows_immediately(store: GrantStore) -> None:
    store.grant(Capability.NETWORK)
    assert store.check(Capability.NETWORK).allowed is True


def test_grant_is_scoped_to_one_capability(store: GrantStore) -> None:
    store.grant(Capability.NETWORK)
    assert store.check(Capability.CAMERA).allowed is False


# -- revocation -------------------------------------------------------------


def test_revocation_takes_effect_immediately(store: GrantStore) -> None:
    store.grant(Capability.VOICE_OUTPUT)
    assert store.check(Capability.VOICE_OUTPUT).allowed is True
    store.revoke(Capability.VOICE_OUTPUT)
    assert store.check(Capability.VOICE_OUTPUT).allowed is False


def test_revocation_is_not_cached(store: GrantStore) -> None:
    """A cached check would make revocation advisory, which is the failure
    mode this whole module exists to avoid."""
    store.grant(Capability.CAMERA)
    for _ in range(3):
        assert store.check(Capability.CAMERA).allowed is True
    store.revoke(Capability.CAMERA)
    for _ in range(3):
        assert store.check(Capability.CAMERA).allowed is False


def test_revoke_all_clears_everything(store: GrantStore) -> None:
    store.grant(Capability.NETWORK)
    store.grant(Capability.CAMERA)
    store.grant(Capability.MICROPHONE)
    assert store.revoke_all() == 3
    assert store.active() == []


def test_revoke_can_target_one_scope(store: GrantStore) -> None:
    store.grant(Capability.NETWORK, scope="data")
    store.grant(Capability.NETWORK, scope="docs")
    store.revoke(Capability.NETWORK, scope="data")
    assert store.check(Capability.NETWORK, "data").allowed is False
    assert store.check(Capability.NETWORK, "docs").allowed is True


def test_revoking_an_ungranted_capability_is_harmless(store: GrantStore) -> None:
    assert store.revoke(Capability.POST_PUBLIC) == 0


# -- scope ------------------------------------------------------------------


def test_a_scoped_grant_allows_its_subtree(store: GrantStore) -> None:
    store.grant(Capability.NETWORK, scope="data")
    assert store.check(Capability.NETWORK, "data").allowed is True
    assert store.check(Capability.NETWORK, "data/product/LISTING.md").allowed is True
    assert store.check(Capability.NETWORK, "data\\product\\x.jsonl").allowed is True


def test_a_scoped_grant_refuses_outside_its_subtree(store: GrantStore) -> None:
    store.grant(Capability.NETWORK, scope="data")
    decision = store.check(Capability.NETWORK, "secrets.txt")
    assert decision.allowed is False
    assert decision.reason is DenyReason.OUT_OF_SCOPE


def test_a_prefix_that_is_not_a_path_boundary_is_refused(store: GrantStore) -> None:
    """`data` must not authorise `database.txt`."""
    store.grant(Capability.NETWORK, scope="data")
    assert store.check(Capability.NETWORK, "database.txt").allowed is False


def test_a_scoped_grant_does_not_authorise_an_empty_scope(store: GrantStore) -> None:
    store.grant(Capability.NETWORK, scope="data")
    assert store.check(Capability.NETWORK, "").allowed is False


def test_an_unscoped_grant_allows_any_scope(store: GrantStore) -> None:
    store.grant(Capability.NETWORK)
    assert store.check(Capability.NETWORK, "anything/at/all").allowed is True


def test_the_tightest_matching_grant_wins(store: GrantStore) -> None:
    store.grant(Capability.NETWORK)
    store.grant(Capability.NETWORK, scope="data")
    decision = store.check(Capability.NETWORK, "data/x.json")
    assert decision.allowed is True
    assert decision.grant is not None
    assert decision.grant.scope == "data"


# -- expiry -----------------------------------------------------------------


def test_an_expired_grant_stops_working(store: GrantStore) -> None:
    store.grant(Capability.CAMERA, hours=8)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    store.connection.execute("UPDATE security_grants SET expires_at = ?", (past,))
    store.connection.commit()
    decision = store.check(Capability.CAMERA)
    assert decision.allowed is False
    assert decision.reason is DenyReason.EXPIRED


def test_a_short_lived_grant_expires(store: GrantStore) -> None:
    # 0.00001 hours is 36ms, so a 60ms sleep genuinely lapses it.
    store.grant(Capability.MICROPHONE, hours=0.00001)
    assert store.check(Capability.MICROPHONE).allowed is True
    import time

    time.sleep(0.06)
    assert store.check(Capability.MICROPHONE).allowed is False


def test_revoke_expired_sweeps_lapsed_grants(store: GrantStore) -> None:
    store.grant(Capability.NETWORK, hours=8)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    store.connection.execute("UPDATE security_grants SET expires_at = ?", (past,))
    store.connection.commit()
    assert store.revoke_expired() == 1
    assert store.active() == []


def test_an_unlimited_grant_reports_itself(store: GrantStore) -> None:
    assert store.grant(Capability.NETWORK).unlimited is True
    assert store.grant(Capability.CAMERA, hours=2).unlimited is False


# -- the capabilities that affect other people ------------------------------


def test_human_affecting_capabilities_are_flagged() -> None:
    for capability in (
        Capability.CAMERA,
        Capability.MICROPHONE,
        Capability.SEND_EMAIL,
        Capability.POST_PUBLIC,
        Capability.MOVE_MONEY,
    ):
        assert capability.needs_human is True
    for capability in (Capability.NETWORK, Capability.VOICE_OUTPUT):
        assert capability.needs_human is False


def test_camera_is_never_permanent_by_accident(store: GrantStore) -> None:
    """Nothing grants itself, so an always-on camera requires a typed grant."""
    assert store.check(Capability.CAMERA).allowed is False
    grant = store.grant(Capability.CAMERA, hours=8, note="standup recording")
    assert grant.unlimited is False
    assert store.report()["needs_human"] == ["CAMERA"]


# -- audit ------------------------------------------------------------------


def test_denials_are_recorded(store: GrantStore) -> None:
    store.check(Capability.MOVE_MONEY)
    store.check(Capability.CAMERA)
    denials = store.denials()
    assert len(denials) == 2
    assert {item["capability"] for item in denials} == {"MOVE_MONEY", "CAMERA"}


def test_denials_carry_context(store: GrantStore) -> None:
    store.check(Capability.SEND_EMAIL, "to=buyer", context={"order": "42"})
    denial = store.denials()[0]
    assert '"order"' in denial["context"]


def test_denial_recording_can_be_disabled(store: GrantStore) -> None:
    store.check(Capability.CAMERA, record_denials=False)
    assert store.denials() == []


def test_allowances_are_not_logged_as_denials(store: GrantStore) -> None:
    store.grant(Capability.NETWORK)
    store.check(Capability.NETWORK)
    assert store.denials() == []


def test_history_keeps_revoked_grants(store: GrantStore) -> None:
    store.grant(Capability.NETWORK)
    store.revoke(Capability.NETWORK)
    assert len(store.history()) == 1
    assert store.history()[0].state == "revoked"


def test_report_separates_granted_from_ungranted(store: GrantStore) -> None:
    store.grant(Capability.NETWORK)
    report = store.report()
    assert "NETWORK" in [g["capability"] for g in report["granted"]]
    assert "CAMERA" in report["ungranted"]


# -- persistence ------------------------------------------------------------


def test_grants_persist_across_sessions(tmp_path: Path) -> None:
    path = tmp_path / "security.db"
    with GrantStore(path) as first:
        first.grant(Capability.VOICE_OUTPUT, note="reads the inbox aloud")
    with GrantStore(path) as second:
        assert second.check(Capability.VOICE_OUTPUT).allowed is True
        second.revoke(Capability.VOICE_OUTPUT)
    with GrantStore(path) as third:
        assert third.check(Capability.VOICE_OUTPUT).allowed is False


def test_revocation_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "security.db"
    with GrantStore(path) as first:
        first.grant(Capability.CAMERA, hours=8)
    with GrantStore(path) as second:
        second.revoke(Capability.CAMERA)
    with GrantStore(path) as third:
        assert third.check(Capability.CAMERA).allowed is False


# -- scope arithmetic -------------------------------------------------------


@pytest.mark.parametrize(
    "granted,asked,expected",
    [
        ("", "anything", True),
        ("", "", True),
        ("data", "data", True),
        ("data", "data/x", True),
        ("data", "database", False),
        ("data", "", False),
        ("data/product", "data/product", True),
        ("data/product", "data/production", False),
        ("a/b", "a/b/c/d", True),
        ("a/b/", "a/b/c", True),
    ],
)
def test_scope_matching(granted: str, asked: str, expected: bool) -> None:
    assert Grant(capability=Capability.NETWORK, scope=granted).covers(asked) is expected


# -- the decision object ----------------------------------------------------


def test_a_decision_serialises(store: GrantStore) -> None:
    store.grant(Capability.NETWORK)
    payload = store.check(Capability.NETWORK, "data").to_dict()
    assert payload["allowed"] is True
    assert payload["grant"]["capability"] == "NETWORK"
    assert payload["grant"]["description"]


def test_a_denial_serialises_with_a_reason(store: GrantStore) -> None:
    payload = store.check(Capability.MOVE_MONEY).to_dict()
    assert payload["allowed"] is False
    assert payload["reason"] == "not_granted"
    assert payload["grant"] is None
