"""A pay-rule lifecycle action decides WHO may act before it says anything about the rule.

``end``, ``notes`` and ``void`` used to load the rule and run state and date checks
(``effective_to < effective_from``, "Only Active rules can be ended", "already Voided") before
establishing that the caller may act on the Driver, so an unrelated caller could probe a rule's
EffectiveFrom, state and existence. The order is now: DRIVER/Self denial; visibility (a caller who
cannot see the rule gets the 404 of a missing rule); rate-edit standing over the Driver (403);
then the existing validations and the locked re-validation. Authorized callers see the same
specific 422/409 as before.
"""
from __future__ import annotations

import pytest

from tests.builders.access import create_company_role_with_permissions, create_user_with_role_token
from tests.builders.transfer_scenarios import auth, sfx
from tests.test_driver_owned_pay_rules import RULES, _calendars, _make
from tests.test_driver_owned_rate_schedules import _manager, _transfer, _world

pytestmark = pytest.mark.asyncio

MISSING = 2_000_000_000
ACTIONS = {
    "end-valid": ("POST", "/end", {"effective_to": "2026-12-31"}),
    "end-before-start": ("POST", "/end", {"effective_to": "2025-01-01"}),
    "notes": ("PATCH", "", {"notes": "probe"}),
    "void": ("POST", "/void", None),
}


def _call(client, token, rule_id, action):
    method, suffix, body = ACTIONS[action]
    return client.request(method, f"{RULES}/{rule_id}{suffix}", json=body, headers=auth(token))


async def _rules_in_every_state(client, token, w) -> dict[str, int]:
    """Active (Min, open), Ended (Max, closed 2026-02-01) and Voided (Max, 2026-03-01 on)."""
    active = await _make(client, token, w, "2026-01-01", rule_type="MinimumPay")
    ended = await _make(client, token, w, "2026-01-01", "2026-06-30", rule_type="MaximumPay")
    closed = await client.post(f"{RULES}/{ended['driver_pay_rule_id']}/end",
                               json={"effective_to": "2026-02-01"}, headers=auth(token))
    assert closed.status_code == 200, closed.text
    voided = await _make(client, token, w, "2026-03-01", "2026-04-30", rule_type="MaximumPay")
    done = await client.post(f"{RULES}/{voided['driver_pay_rule_id']}/void", headers=auth(token))
    assert done.status_code == 200, done.text
    return {"Active": active["driver_pay_rule_id"], "Ended": ended["driver_pay_rule_id"],
            "Voided": voided["driver_pay_rule_id"]}


async def test_an_unrelated_branch_caller_cannot_tell_a_rule_from_a_missing_one(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w)
    rules = await _rules_in_every_state(client, auth_token, w)

    for action in ACTIONS:
        missing = await _call(client, w.other_manager, MISSING, action)
        assert missing.status_code == 404, (action, missing.text)
        for state, rule_id in rules.items():
            response = await _call(client, w.other_manager, rule_id, action)
            assert (response.status_code, response.json()) == (missing.status_code, missing.json()), (
                action, state, response.text)
            text_ = response.text
            for leaked in ("2026-01-01", "2026-03-01", "Active", "Ended", "Voided", "status"):
                assert leaked not in text_, (action, state, text_)


async def test_a_driver_self_account_is_denied_before_anything_about_the_rule(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w)
    rules = await _rules_in_every_state(client, auth_token, w)
    roles = (await client.get("/admin/company-roles", headers=auth(auth_token))).json()
    driver_role = next(r["company_role_id"] for r in roles if r["role_code"] == "DRIVER")
    self_token = await create_user_with_role_token(
        client, auth_token, f"dos_self_{sfx()}", driver_role,
        scope_type="Self", driver_branch_id=w.old_branch, password="TestPass1234!")

    for action in ACTIONS:
        outcomes = {
            (await _call(client, self_token, rule_id, action)).text
            for rule_id in (*rules.values(), MISSING)
        }
        assert len(outcomes) == 1, (action, outcomes)  # identical for every state and a missing rule
        response = await _call(client, self_token, rules["Active"], action)
        assert response.status_code == 403, (action, response.text)
        assert "DRIVER Self" in response.text


async def test_a_past_branch_reader_is_refused_with_403_and_no_state_leakage(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w)
    rules = await _rules_in_every_state(client, auth_token, w)
    viewer_role = await create_company_role_with_permissions(
        client, auth_token, f"DOS viewer {sfx()}", ["drivers.view", "payrates.view"])
    viewer = await _manager(client, auth_token, viewer_role, w.old_branch)
    await _transfer(client, w)  # the old Branch is now a past Branch: the viewer reads, never edits

    # It can still see the rules (read scope) ...
    for rule_id in rules.values():
        assert (await client.get(f"{RULES}/{rule_id}", headers=auth(viewer))).status_code == 200
    # ... and every action is the same 403, whatever the rule's state or the supplied dates.
    for action in ACTIONS:
        bodies = set()
        for state, rule_id in rules.items():
            response = await _call(client, viewer, rule_id, action)
            assert response.status_code == 403, (action, state, response.text)
            bodies.add(response.text)
        assert len(bodies) == 1, (action, bodies)


async def test_authorized_callers_still_get_the_specific_validation_errors(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w)
    rules = await _rules_in_every_state(client, auth_token, w)
    for token in (auth_token, w.old_manager):
        before = await _call(client, token, rules["Active"], "end-before-start")
        assert before.status_code == 422 and "cannot be before effective_from" in before.text
        for state in ("Ended", "Voided"):
            refused = await _call(client, token, rules[state], "end-valid")
            assert refused.status_code == 422 and f"current status: '{state}'" in refused.text, state
        void = await _call(client, token, rules["Voided"], "void")
        assert void.status_code == 422 and "already Voided" in void.text
        notes = await _call(client, token, rules["Voided"], "notes")
        assert notes.status_code == 422 and "Voided" in notes.text
    assert (await _call(client, w.old_manager, rules["Active"], "notes")).status_code == 200
    assert (await _call(client, auth_token, rules["Active"], "end-valid")).status_code == 200
