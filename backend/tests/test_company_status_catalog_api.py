"""Company Status catalog API: names, aliases, Branch usage limits and Status rate columns.

The tests share the seeded Company, so every Status and column gets a unique name.
"""
from uuid import uuid4

import httpx
import pytest

from app.settings.status_names import normalize_status_name
from tests.test_settings_payroll import auth, make_key, uname

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "normalized"), [
    ("Vacation", "vacation"),
    ("  VACATION  ", "vacation"),
    ("Sick   Day", "sick day"),
    ("Sick\tDay\n", "sick day"),
    ("Ｖａｃａｔｉｏｎ", "vacation"),        # full-width letters (NFKC)
    ("ＳＩＣＫ　ＤＡＹ", "sick day"),         # full-width space (NFKC)
    ("Straße", "strasse"),                    # case folding
    (" Jury Duty ", "jury duty"),
    ("", ""),
    ("   ", ""),
])
def test_names_and_aliases_share_one_normal_form(raw, normalized):
    assert normalize_status_name(raw) == normalized


@pytest.mark.parametrize("raw", ["Sick Day", "ＳＩＣＫ　ＤＡＹ", "Straße  Ⅻ", "ﬁne  Print"])
def test_normalization_is_idempotent(raw):
    once = normalize_status_name(raw)
    assert normalize_status_name(once) == once


# ---------------------------------------------------------------------------
# Name uniqueness
# ---------------------------------------------------------------------------

class TestCanonicalNameUniqueness:

    @pytest.mark.parametrize("variant", [
        lambda n: n, lambda n: n.upper(), lambda n: f"  {n}  ",
        lambda n: n.replace(" ", "   "),
    ])
    async def test_the_same_normalized_name_is_refused(
        self, client: httpx.AsyncClient, auth_token: str, variant,
    ):
        name = uname("Annual leave")
        await make_key(client, auth_token, key_name=name)
        resp = await client.post(
            "/settings/status-keys",
            json={"key_name": variant(name), "category": "Vacation"}, headers=auth(auth_token))
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "STATUS_NAME_NOT_UNIQUE"

    async def test_fullwidth_form_of_a_name_is_the_same_name(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        suffix = uuid4().hex[:6]
        await make_key(client, auth_token, key_name=f"Sick {suffix}")
        resp = await client.post(
            "/settings/status-keys",
            json={"key_name": f"ＳＩＣＫ　{suffix.upper()}", "category": "Sick"},
            headers=auth(auth_token))
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "STATUS_NAME_NOT_UNIQUE"

    async def test_renaming_to_an_existing_name_is_refused_and_keeps_the_old_name(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        taken = await make_key(client, auth_token)
        other = await make_key(client, auth_token)
        resp = await client.patch(
            f"/settings/status-keys/{other['status_key_id']}",
            json={"key_name": taken["key_name"].upper()}, headers=auth(auth_token))
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "STATUS_NAME_NOT_UNIQUE"
        listed = (await client.get("/settings/status-keys", headers=auth(auth_token))).json()
        assert next(k for k in listed if k["status_key_id"] == other["status_key_id"])["key_name"] \
            == other["key_name"]

    async def test_a_status_can_be_renamed_to_a_different_case_of_its_own_name(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        key = await make_key(client, auth_token)
        resp = await client.patch(
            f"/settings/status-keys/{key['status_key_id']}",
            json={"key_name": key["key_name"].upper()}, headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json()["key_name"] == key["key_name"].upper()

    async def test_a_deactivated_name_does_not_block_and_reactivation_rechecks(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        name = uname("Seasonal")
        old = await make_key(client, auth_token, key_name=name)
        await client.delete(f"/settings/status-keys/{old['status_key_id']}", headers=auth(auth_token))
        new = await make_key(client, auth_token, key_name=name)
        assert new["status_key_id"] != old["status_key_id"]

        resp = await client.patch(
            f"/settings/status-keys/{old['status_key_id']}",
            json={"is_active": True}, headers=auth(auth_token))
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "STATUS_NAME_NOT_UNIQUE"

        # Once the new Status is deactivated the old one can come back.
        await client.delete(f"/settings/status-keys/{new['status_key_id']}", headers=auth(auth_token))
        again = await client.patch(
            f"/settings/status-keys/{old['status_key_id']}",
            json={"is_active": True}, headers=auth(auth_token))
        assert again.status_code == 200
        assert again.json()["is_active"] is True


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------

class TestAliases:

    async def test_aliases_are_stored_as_typed_in_creation_order(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        tag = uuid4().hex[:6]
        key = await make_key(client, auth_token, aliases=[f"Vac {tag}", f"AL {tag}", f"Hol {tag}"])
        assert key["aliases"] == [f"Vac {tag}", f"AL {tag}", f"Hol {tag}"]

    async def test_there_is_no_domain_maximum_on_aliases(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        tag = uuid4().hex[:6]
        aliases = [f"alias-{index}-{tag}" for index in range(60)]
        key = await make_key(client, auth_token, aliases=aliases)
        assert key["aliases"] == aliases

    async def test_the_same_alias_sent_twice_is_one_alias(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        tag = uuid4().hex[:6]
        key = await make_key(client, auth_token, aliases=[f"Vac {tag}", f"  VAC   {tag} "])
        assert key["aliases"] == [f"Vac {tag}"]

    async def test_a_blank_alias_is_rejected(self, client: httpx.AsyncClient, auth_token: str):
        resp = await client.post(
            "/settings/status-keys",
            json={"key_name": uname(), "category": "Vacation", "aliases": ["ok", "  "]},
            headers=auth(auth_token))
        assert resp.status_code == 422

    async def test_an_alias_cannot_equal_another_active_status_name(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        other = await make_key(client, auth_token)
        resp = await client.post(
            "/settings/status-keys",
            json={"key_name": uname(), "category": "Vacation",
                  "aliases": [other["key_name"].upper()]},
            headers=auth(auth_token))
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "STATUS_ALIAS_AMBIGUOUS"
        assert other["key_name"] in resp.json()["detail"]["message"]

    async def test_an_alias_cannot_equal_another_active_status_alias(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        tag = uuid4().hex[:6]
        await make_key(client, auth_token, aliases=[f"Vac {tag}"])
        resp = await client.post(
            "/settings/status-keys",
            json={"key_name": uname(), "category": "Vacation", "aliases": [f"VAC  {tag}"]},
            headers=auth(auth_token))
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "STATUS_ALIAS_AMBIGUOUS"

    async def test_an_alias_cannot_equal_its_own_name(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        name = uname()
        resp = await client.post(
            "/settings/status-keys",
            json={"key_name": name, "category": "Vacation", "aliases": [name.lower()]},
            headers=auth(auth_token))
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "STATUS_ALIAS_AMBIGUOUS"

    async def test_a_name_cannot_equal_an_active_alias_of_another_status(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        tag = uuid4().hex[:6]
        await make_key(client, auth_token, aliases=[f"Holiday {tag}"])
        resp = await client.post(
            "/settings/status-keys",
            json={"key_name": f"HOLIDAY {tag}", "category": "Holiday"}, headers=auth(auth_token))
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "STATUS_ALIAS_AMBIGUOUS"

    async def test_renaming_a_status_to_one_of_its_own_aliases_is_refused(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        tag = uuid4().hex[:6]
        key = await make_key(client, auth_token, aliases=[f"Vac {tag}"])
        resp = await client.patch(
            f"/settings/status-keys/{key['status_key_id']}",
            json={"key_name": f"Vac {tag}"}, headers=auth(auth_token))
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "STATUS_ALIAS_AMBIGUOUS"

    async def test_the_aliases_of_a_deactivated_status_do_not_block(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        tag = uuid4().hex[:6]
        old = await make_key(client, auth_token, aliases=[f"Vac {tag}"])
        await client.delete(f"/settings/status-keys/{old['status_key_id']}", headers=auth(auth_token))
        new = await make_key(client, auth_token, aliases=[f"Vac {tag}"])
        assert new["aliases"] == [f"Vac {tag}"]

    async def test_the_name_of_a_deactivated_status_does_not_block_an_alias(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        old = await make_key(client, auth_token)
        await client.delete(f"/settings/status-keys/{old['status_key_id']}", headers=auth(auth_token))
        new = await make_key(client, auth_token, aliases=[old["key_name"]])
        assert new["aliases"] == [old["key_name"]]

    async def test_reactivation_rechecks_the_aliases_of_the_status(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        tag = uuid4().hex[:6]
        old = await make_key(client, auth_token, aliases=[f"Vac {tag}"])
        await client.delete(f"/settings/status-keys/{old['status_key_id']}", headers=auth(auth_token))
        await make_key(client, auth_token, aliases=[f"Vac {tag}"])
        resp = await client.patch(
            f"/settings/status-keys/{old['status_key_id']}",
            json={"is_active": True}, headers=auth(auth_token))
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "STATUS_ALIAS_AMBIGUOUS"

    async def test_patch_replaces_the_whole_alias_set(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        tag = uuid4().hex[:6]
        key = await make_key(client, auth_token, aliases=[f"A {tag}", f"B {tag}"])
        resp = await client.patch(
            f"/settings/status-keys/{key['status_key_id']}",
            json={"aliases": [f"B {tag}", f"C {tag}"]}, headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json()["aliases"] == [f"B {tag}", f"C {tag}"]
        cleared = await client.patch(
            f"/settings/status-keys/{key['status_key_id']}",
            json={"aliases": []}, headers=auth(auth_token))
        assert cleared.json()["aliases"] == []

    async def test_a_patch_without_aliases_keeps_them(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        tag = uuid4().hex[:6]
        key = await make_key(client, auth_token, aliases=[f"A {tag}"])
        resp = await client.patch(
            f"/settings/status-keys/{key['status_key_id']}",
            json={"display_order": 3}, headers=auth(auth_token))
        assert resp.json()["aliases"] == [f"A {tag}"]

    async def test_a_failed_alias_replacement_changes_nothing(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        """The replacement is atomic: one ambiguous alias keeps the previous set intact."""
        tag = uuid4().hex[:6]
        other = await make_key(client, auth_token, aliases=[f"Taken {tag}"])
        key = await make_key(client, auth_token, aliases=[f"Mine {tag}"])
        resp = await client.patch(
            f"/settings/status-keys/{key['status_key_id']}",
            json={"aliases": [f"Fresh {tag}", f"Taken {tag}"]}, headers=auth(auth_token))
        assert resp.status_code == 422
        listed = (await client.get("/settings/status-keys", headers=auth(auth_token))).json()
        mine = next(k for k in listed if k["status_key_id"] == key["status_key_id"])
        assert mine["aliases"] == [f"Mine {tag}"]
        assert other["status_key_id"] != key["status_key_id"]

    async def test_a_swap_of_name_and_alias_in_one_request_is_allowed(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        tag = uuid4().hex[:6]
        key = await make_key(client, auth_token, key_name=f"Alpha {tag}", aliases=[f"Beta {tag}"])
        resp = await client.patch(
            f"/settings/status-keys/{key['status_key_id']}",
            json={"key_name": f"Beta {tag}", "aliases": [f"Alpha {tag}"]}, headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json()["key_name"] == f"Beta {tag}"
        assert resp.json()["aliases"] == [f"Alpha {tag}"]

    async def test_alias_text_can_be_edited_without_changing_the_identity(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        tag = uuid4().hex[:6]
        key = await make_key(client, auth_token, aliases=[f"vac {tag}"])
        resp = await client.patch(
            f"/settings/status-keys/{key['status_key_id']}",
            json={"aliases": [f"VAC {tag}"]}, headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json()["aliases"] == [f"VAC {tag}"]
        assert resp.json()["status_key_id"] == key["status_key_id"]

    async def test_a_deactivated_status_releases_its_aliases(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        tag = uuid4().hex[:6]
        key = await make_key(client, auth_token, aliases=[f"Vac {tag}"])
        await client.delete(f"/settings/status-keys/{key['status_key_id']}", headers=auth(auth_token))
        resp = await client.post(
            "/settings/status-keys",
            json={"key_name": uname(), "category": "Vacation", "aliases": [f"Vac {tag}"]},
            headers=auth(auth_token))
        assert resp.status_code == 201

    async def test_alias_writes_need_company_level_authority(
        self, client: httpx.AsyncClient, auth_token: str, branch_user_token: str,
    ):
        key = await make_key(client, auth_token)
        resp = await client.patch(
            f"/settings/status-keys/{key['status_key_id']}",
            json={"aliases": ["sneaky"]}, headers=auth(branch_user_token))
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Branch usage limits
# ---------------------------------------------------------------------------

def _limits_url(branch_id: int, key_id: int | None = None) -> str:
    if key_id is None:
        return f"/settings/branches/{branch_id}/status-key-limits"
    return f"/settings/branches/{branch_id}/status-keys/{key_id}/limits"


def _entry(listing: list[dict], key_id: int) -> dict:
    return next(row for row in listing if row["status_key_id"] == key_id)


class TestBranchLimits:

    async def test_a_status_without_a_row_lists_every_limit_disabled(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int,
    ):
        key = await make_key(client, auth_token)
        listing = (await client.get(_limits_url(hq_branch_id), headers=auth(auth_token))).json()
        row = _entry(listing, key["status_key_id"])
        assert row["key_name"] == key["key_name"]
        assert row["branch_id"] == hq_branch_id
        for field in ("per_period", "per_driver", "across_drivers", "per_day"):
            assert row[f"limit_uses_{field}_enabled"] is False
            assert row[f"limit_uses_{field}"] is None

    async def test_the_four_limits_are_stored_for_the_branch(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int,
    ):
        key = await make_key(client, auth_token)
        resp = await client.put(
            _limits_url(hq_branch_id, key["status_key_id"]),
            json={
                "limit_uses_per_period_enabled": True, "limit_uses_per_period": 5,
                "limit_uses_per_driver_enabled": True, "limit_uses_per_driver": 2,
                "limit_uses_across_drivers_enabled": True, "limit_uses_across_drivers": 3,
                "limit_uses_per_day_enabled": True, "limit_uses_per_day": 1,
            }, headers=auth(auth_token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert (body["limit_uses_per_period"], body["limit_uses_per_driver"],
                body["limit_uses_across_drivers"], body["limit_uses_per_day"]) == (5, 2, 3, 1)
        listing = (await client.get(_limits_url(hq_branch_id), headers=auth(auth_token))).json()
        assert _entry(listing, key["status_key_id"]) == body

    async def test_limits_are_independent_per_branch(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int,
        seeded_paytest_branch_id: int,
    ):
        key = await make_key(client, auth_token)
        await client.put(
            _limits_url(hq_branch_id, key["status_key_id"]),
            json={"limit_uses_per_day_enabled": True, "limit_uses_per_day": 1},
            headers=auth(auth_token))
        other = (await client.get(
            _limits_url(seeded_paytest_branch_id), headers=auth(auth_token))).json()
        assert _entry(other, key["status_key_id"])["limit_uses_per_day_enabled"] is False
        await client.put(
            _limits_url(seeded_paytest_branch_id, key["status_key_id"]),
            json={"limit_uses_per_period_enabled": True, "limit_uses_per_period": 9},
            headers=auth(auth_token))
        hq = (await client.get(_limits_url(hq_branch_id), headers=auth(auth_token))).json()
        assert _entry(hq, key["status_key_id"])["limit_uses_per_day"] == 1
        assert _entry(hq, key["status_key_id"])["limit_uses_per_period_enabled"] is False

    async def test_a_put_replaces_the_whole_set_of_limits(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int,
    ):
        key = await make_key(client, auth_token)
        url = _limits_url(hq_branch_id, key["status_key_id"])
        await client.put(url, json={"limit_uses_per_day_enabled": True, "limit_uses_per_day": 4},
                         headers=auth(auth_token))
        resp = await client.put(
            url, json={"limit_uses_per_driver_enabled": True, "limit_uses_per_driver": 2},
            headers=auth(auth_token))
        body = resp.json()
        assert body["limit_uses_per_day_enabled"] is False
        assert body["limit_uses_per_day"] is None
        assert body["limit_uses_per_driver"] == 2

    async def test_disabling_every_limit_removes_the_row(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, db_conn,
    ):
        from sqlalchemy import text

        key = await make_key(client, auth_token)
        url = _limits_url(hq_branch_id, key["status_key_id"])
        await client.put(url, json={"limit_uses_per_day_enabled": True, "limit_uses_per_day": 4},
                         headers=auth(auth_token))
        count = "SELECT count(*) FROM payroll.payrollstatuskeybranchlimits WHERE statuskeyid = :k"
        assert (await db_conn.execute(text(count), {"k": key["status_key_id"]})).scalar_one() == 1

        resp = await client.put(url, json={}, headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json()["limit_uses_per_day_enabled"] is False
        assert (await db_conn.execute(text(count), {"k": key["status_key_id"]})).scalar_one() == 0

    async def test_the_limit_listing_has_only_active_statuses(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int,
    ):
        key = await make_key(client, auth_token)
        await client.delete(f"/settings/status-keys/{key['status_key_id']}", headers=auth(auth_token))
        listing = (await client.get(_limits_url(hq_branch_id), headers=auth(auth_token))).json()
        assert key["status_key_id"] not in [row["status_key_id"] for row in listing]

    @pytest.mark.parametrize("field", ["per_period", "per_driver", "across_drivers", "per_day"])
    @pytest.mark.parametrize("value", [0, -1])
    async def test_an_enabled_limit_must_be_positive(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, field, value,
    ):
        key = await make_key(client, auth_token)
        resp = await client.put(
            _limits_url(hq_branch_id, key["status_key_id"]),
            json={f"limit_uses_{field}_enabled": True, f"limit_uses_{field}": value},
            headers=auth(auth_token))
        assert resp.status_code == 422

    async def test_an_enabled_limit_needs_a_value(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int,
    ):
        key = await make_key(client, auth_token)
        resp = await client.put(
            _limits_url(hq_branch_id, key["status_key_id"]),
            json={"limit_uses_per_day_enabled": True}, headers=auth(auth_token))
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "STATUS_LIMIT_VALUE_REQUIRED"

    async def test_unknown_branch_and_status_are_not_found(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int,
    ):
        key = await make_key(client, auth_token)
        body = {"limit_uses_per_day_enabled": True, "limit_uses_per_day": 1}
        assert (await client.put(_limits_url(999999, key["status_key_id"]), json=body,
                                 headers=auth(auth_token))).status_code == 404
        assert (await client.put(_limits_url(hq_branch_id, 999999), json=body,
                                 headers=auth(auth_token))).status_code == 404
        assert (await client.get(_limits_url(999999), headers=auth(auth_token))).status_code == 404

    async def test_writing_limits_needs_company_level_authority(
        self, client: httpx.AsyncClient, auth_token: str, branch_user_token: str,
        hq_branch_id: int,
    ):
        key = await make_key(client, auth_token)
        resp = await client.put(
            _limits_url(hq_branch_id, key["status_key_id"]),
            json={"limit_uses_per_day_enabled": True, "limit_uses_per_day": 1},
            headers=auth(branch_user_token))
        assert resp.status_code == 403

    async def test_a_branch_user_reads_only_their_own_branch_limits(
        self, client: httpx.AsyncClient, branch_user_token: str, hq_branch_id: int,
        seeded_paytest_branch_id: int,
    ):
        assert (await client.get(
            _limits_url(hq_branch_id), headers=auth(branch_user_token))).status_code == 200
        assert (await client.get(
            _limits_url(seeded_paytest_branch_id), headers=auth(branch_user_token))).status_code == 403

    async def test_limits_survive_a_status_rename_and_follow_its_identity(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int,
    ):
        key = await make_key(client, auth_token)
        await client.put(
            _limits_url(hq_branch_id, key["status_key_id"]),
            json={"limit_uses_per_day_enabled": True, "limit_uses_per_day": 2},
            headers=auth(auth_token))
        await client.patch(
            f"/settings/status-keys/{key['status_key_id']}",
            json={"key_name": uname("Renamed")}, headers=auth(auth_token))
        listing = (await client.get(_limits_url(hq_branch_id), headers=auth(auth_token))).json()
        assert _entry(listing, key["status_key_id"])["limit_uses_per_day"] == 2


# ---------------------------------------------------------------------------
# Status rate columns
# ---------------------------------------------------------------------------

async def _columns(client, token, **params) -> list[dict]:
    resp = await client.get("/settings/status-rate-columns", params=params, headers=auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def make_column(client, token, **overrides) -> dict:
    body = {"column_name": uname("Column")} | overrides
    resp = await client.post("/settings/status-rate-columns", json=body, headers=auth(token))
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestStatusRateColumns:

    async def test_a_company_has_exactly_one_default_active_column(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        columns = await _columns(client, auth_token)
        defaults = [c for c in columns if c["is_default"] and c["is_active"]]
        assert len(defaults) == 1
        assert "branch_id" not in defaults[0]
        assert "rate_type_id" not in defaults[0]

    async def test_a_column_is_visible_to_every_branch_user(
        self, client: httpx.AsyncClient, auth_token: str, branch_user_token: str,
    ):
        column = await make_column(client, auth_token)
        seen = await _columns(client, branch_user_token)
        assert column["status_rate_column_id"] in [c["status_rate_column_id"] for c in seen]

    async def test_creating_a_column_creates_its_rate_definition_and_component(
        self, client: httpx.AsyncClient, auth_token: str, db_conn,
    ):
        from sqlalchemy import text

        column = await make_column(client, auth_token)
        assert column["is_default"] is False
        assert column["is_active"] is True
        rows = (await db_conn.execute(text("""
            SELECT rd.ratedefinitionid, rd.shape, rd.paydefinitionid, rd.structurelockedatutc,
                   (SELECT count(*) FROM payroll.ratecomponentdefinitions c
                     WHERE c.ratedefinitionid = rd.ratedefinitionid) AS components
            FROM payroll.ratedefinitions rd WHERE rd.statusratecolumnid = :c
        """), {"c": column["status_rate_column_id"]})).mappings().all()
        assert len(rows) == 1
        assert rows[0]["ratedefinitionid"] == column["rate_definition_id"]
        assert rows[0]["shape"] == "Scalar"
        assert rows[0]["paydefinitionid"] is None
        assert rows[0]["structurelockedatutc"] is not None
        assert rows[0]["components"] == 1

    async def test_column_names_are_unique_among_active_columns(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        column = await make_column(client, auth_token)
        resp = await client.post(
            "/settings/status-rate-columns",
            json={"column_name": f"  {column['column_name'].upper()} "}, headers=auth(auth_token))
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "STATUS_RATE_COLUMN_NAME_NOT_UNIQUE"

    async def test_a_deactivated_column_name_can_be_reused(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        column = await make_column(client, auth_token)
        gone = await client.delete(
            f"/settings/status-rate-columns/{column['status_rate_column_id']}",
            headers=auth(auth_token))
        assert gone.status_code == 200
        assert gone.json()["is_active"] is False
        again = await make_column(client, auth_token, column_name=column["column_name"])
        assert again["status_rate_column_id"] != column["status_rate_column_id"]

    async def test_rename_and_reactivate_a_column(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        column = await make_column(client, auth_token)
        url = f"/settings/status-rate-columns/{column['status_rate_column_id']}"
        renamed = await client.patch(url, json={"column_name": uname("Renamed")}, headers=auth(auth_token))
        assert renamed.status_code == 200
        assert renamed.json()["rate_definition_id"] == column["rate_definition_id"]
        await client.delete(url, headers=auth(auth_token))
        back = await client.patch(url, json={"is_active": True}, headers=auth(auth_token))
        assert back.status_code == 200
        assert back.json()["is_active"] is True

    async def test_reactivating_a_column_rechecks_its_name(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        old = await make_column(client, auth_token)
        url = f"/settings/status-rate-columns/{old['status_rate_column_id']}"
        await client.delete(url, headers=auth(auth_token))
        await make_column(client, auth_token, column_name=old["column_name"])
        resp = await client.patch(url, json={"is_active": True}, headers=auth(auth_token))
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "STATUS_RATE_COLUMN_NAME_NOT_UNIQUE"

    async def test_making_another_column_the_default_demotes_the_old_default(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        before = next(c for c in await _columns(client, auth_token) if c["is_default"])
        column = await make_column(client, auth_token)
        resp = await client.patch(
            f"/settings/status-rate-columns/{column['status_rate_column_id']}",
            json={"is_default": True}, headers=auth(auth_token))
        assert resp.status_code == 200
        after = await _columns(client, auth_token)
        assert [c["status_rate_column_id"] for c in after if c["is_default"]] == [
            column["status_rate_column_id"]]
        # Restore the original default for the tests that follow.
        restored = await client.patch(
            f"/settings/status-rate-columns/{before['status_rate_column_id']}",
            json={"is_default": True}, headers=auth(auth_token))
        assert restored.status_code == 200

    async def test_creating_a_default_column_demotes_the_old_default(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        before = next(c for c in await _columns(client, auth_token) if c["is_default"])
        column = await make_column(client, auth_token, is_default=True)
        after = await _columns(client, auth_token)
        assert [c["status_rate_column_id"] for c in after if c["is_default"]] == [
            column["status_rate_column_id"]]
        await client.patch(
            f"/settings/status-rate-columns/{before['status_rate_column_id']}",
            json={"is_default": True}, headers=auth(auth_token))

    async def test_the_default_column_cannot_be_cleared_or_deactivated(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        default = next(c for c in await _columns(client, auth_token) if c["is_default"])
        url = f"/settings/status-rate-columns/{default['status_rate_column_id']}"
        for call in (
            client.patch(url, json={"is_default": False}, headers=auth(auth_token)),
            client.patch(url, json={"is_active": False}, headers=auth(auth_token)),
            client.delete(url, headers=auth(auth_token)),
        ):
            resp = await call
            assert resp.status_code == 409
            assert resp.json()["detail"]["code"] == "STATUS_RATE_COLUMN_IS_DEFAULT"

    async def test_a_column_bound_to_an_active_status_cannot_be_deactivated(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        column = await make_column(client, auth_token)
        key = await make_key(
            client, auth_token, hours_value="8",
            status_rate_column_id=column["status_rate_column_id"])
        url = f"/settings/status-rate-columns/{column['status_rate_column_id']}"
        resp = await client.delete(url, headers=auth(auth_token))
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "STATUS_RATE_COLUMN_IN_USE"
        await client.delete(f"/settings/status-keys/{key['status_key_id']}", headers=auth(auth_token))
        assert (await client.delete(url, headers=auth(auth_token))).status_code == 200

    async def test_a_status_cannot_bind_an_inactive_column(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        column = await make_column(client, auth_token)
        await client.delete(
            f"/settings/status-rate-columns/{column['status_rate_column_id']}",
            headers=auth(auth_token))
        resp = await client.post(
            "/settings/status-keys",
            json={"key_name": uname(), "category": "Vacation", "hours_value": "8",
                  "status_rate_column_id": column["status_rate_column_id"]},
            headers=auth(auth_token))
        assert resp.status_code == 422

    async def test_inactive_columns_are_listed_only_on_request(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        column = await make_column(client, auth_token)
        await client.delete(
            f"/settings/status-rate-columns/{column['status_rate_column_id']}",
            headers=auth(auth_token))
        ids = lambda cols: [c["status_rate_column_id"] for c in cols]  # noqa: E731
        assert column["status_rate_column_id"] not in ids(await _columns(client, auth_token))
        assert column["status_rate_column_id"] in ids(
            await _columns(client, auth_token, include_inactive="true"))

    async def test_writes_need_company_level_authority(
        self, client: httpx.AsyncClient, auth_token: str, branch_user_token: str,
    ):
        column = await make_column(client, auth_token)
        url = f"/settings/status-rate-columns/{column['status_rate_column_id']}"
        assert (await client.post(
            "/settings/status-rate-columns", json={"column_name": uname()},
            headers=auth(branch_user_token))).status_code == 403
        assert (await client.patch(
            url, json={"column_name": uname()}, headers=auth(branch_user_token))).status_code == 403
        assert (await client.delete(url, headers=auth(branch_user_token))).status_code == 403

    async def test_an_unknown_column_is_not_found(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        assert (await client.patch(
            "/settings/status-rate-columns/999999", json={"column_name": "x"},
            headers=auth(auth_token))).status_code == 404
        assert (await client.delete(
            "/settings/status-rate-columns/999999", headers=auth(auth_token))).status_code == 404

    async def test_a_blank_column_name_is_rejected(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        resp = await client.post(
            "/settings/status-rate-columns", json={"column_name": "  "}, headers=auth(auth_token))
        assert resp.status_code == 422


class TestBranchesNoLongerOwnTheStatusCatalog:

    async def test_the_branch_scoped_routes_are_gone(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int,
    ):
        for method, url in (
            ("GET", f"/settings/branches/{hq_branch_id}/status-keys"),
            ("POST", f"/settings/branches/{hq_branch_id}/status-keys"),
            ("GET", f"/settings/branches/{hq_branch_id}/status-rate-columns"),
            ("POST", f"/settings/branches/{hq_branch_id}/status-rate-columns"),
        ):
            resp = await client.request(method, url, headers=auth(auth_token))
            assert resp.status_code in (404, 405), (method, url, resp.status_code)

    async def test_a_new_branch_has_the_company_catalog_without_any_setup(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        key = await make_key(client, auth_token)
        branch = await client.post(
            "/settings/branches",
            json={"branch_name": uname("Late branch"), "time_zone_id": "UTC",
                  "branch_code": "LB" + uuid4().hex[:6].upper()},
            headers=auth(auth_token))
        assert branch.status_code == 201, branch.text
        listing = (await client.get(
            _limits_url(branch.json()["branch_id"]), headers=auth(auth_token))).json()
        assert key["status_key_id"] in [row["status_key_id"] for row in listing]
        assert "status_keys_count" not in branch.json()
