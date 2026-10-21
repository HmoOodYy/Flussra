"""The Company Status catalog: lifecycle, aliases, the semantic lock and input resolution.

A Status key belongs to the Company, so every Branch shares one catalog. It has one canonical
primary name (the name every display shows), any number of aliases (input shortcuts only), a
fixed Category, the hours a day of it stands for, whether it counts as a full day off, and an
optional Status rate column that makes it monetary.

Uniqueness: the normalized primary name is unique among a Company's active Statuses, and a
normalized alias is unambiguous across the active names and aliases of the Company. The
database enforces both (see migration 0090); this module checks first so the answer is a clear
422 (`STATUS_NAME_NOT_UNIQUE` / `STATUS_ALIAS_AMBIGUOUS`) and translates the database's
answer when a concurrent writer wins the race.

Once any day-entry selection or frozen Status evidence references a Status, its hours, off-day
semantic, Category and rate column binding cannot change (409 `STATUS_KEY_IN_USE`): deactivate
it and create a new Status. Name, display order, active flag and aliases stay editable.
"""
import random as _random
import string as _string
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.branch_access import check_branch_access
from app.settings import audit, security, status_rate_columns
from app.settings.schemas import StatusKey, StatusKeyCreate, StatusKeyUpdate
from app.settings.status_names import STATUS_NAME_MAX_LENGTH, normalize_status_name

_SK_CODE_CHARS = _string.ascii_uppercase + _string.digits
_CODE_ATTEMPTS = 8

_STATUS_KEY_COLS = """
    k.statuskeyid, k.companyid, k.keyname, k.statuscode, k.normalizedstatuscode,
    k.normalizedkeyname, k.hoursvalue, k.isoffreason, k.deductsfromyearlyallowance,
    k.allowancecategory, k.isactive, k.displayorder, k.statusratecolumnid,
    k.createdatutc, k.updatedatutc
"""

_IN_USE_MESSAGE = (
    "This Status has been selected, so its hours, full-day-off setting, Category and rate "
    "column cannot change. Deactivate it and create a new Status."
)


def _coded(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _generate_status_key_code() -> str:
    """A random SK_XXXXXXXX technical code (8 uppercase alphanumerics)."""
    return "SK_" + "".join(_random.choices(_SK_CODE_CHARS, k=8))


def _normalized_name(value: str, label: str) -> str:
    normalized = normalize_status_name(value)
    if not normalized:
        raise _coded(422, "STATUS_NAME_REQUIRED", f"{label} must not be blank.")
    if len(normalized) > STATUS_NAME_MAX_LENGTH:
        raise _coded(422, "STATUS_NAME_TOO_LONG", f"{label} is too long.")
    return normalized


def _alias_map(aliases: list[str]) -> dict[str, str]:
    """Normalized alias -> the alias text as typed. The same alias sent twice is one alias."""
    result: dict[str, str] = {}
    for alias in aliases:
        result.setdefault(_normalized_name(alias, "An alias"), alias.strip())
    return result


def _row_to_status_key(row, aliases: list[str], in_use: bool) -> StatusKey:
    return StatusKey(
        status_key_id=row["statuskeyid"],
        company_id=row["companyid"],
        key_name=row["keyname"],
        aliases=aliases,
        status_code=row["statuscode"],
        category=row["allowancecategory"],
        hours_value=row["hoursvalue"],
        is_off_reason=bool(row["isoffreason"]),
        deducts_from_yearly_allowance=bool(row["deductsfromyearlyallowance"]),
        is_active=bool(row["isactive"]),
        display_order=int(row["displayorder"] or 0),
        status_rate_column_id=row["statusratecolumnid"],
        is_in_use=in_use,
        created_at_utc=row["createdatutc"],
        updated_at_utc=row["updatedatutc"],
    )


def _database_conflict(exc: DBAPIError) -> HTTPException | None:
    """The coded error for an answer the database gave (a concurrent writer won the race)."""
    message = str(exc.orig) if exc.orig is not None else str(exc)
    lowered = message.lower()
    if "STATUS_ALIAS_AMBIGUOUS" in message or "ux_payrollstatuskeyaliases_activealias" in lowered:
        return _coded(422, "STATUS_ALIAS_AMBIGUOUS",
                      "An alias would be ambiguous: it equals a name or alias of another Status.")
    if "STATUS_NAME_NOT_UNIQUE" in message or "ux_payrollstatuskeys_activename" in lowered:
        return _coded(422, "STATUS_NAME_NOT_UNIQUE",
                      "An active Status of this Company already has this name.")
    if "STATUS_KEY_IN_USE" in message:
        return _coded(409, "STATUS_KEY_IN_USE", _IN_USE_MESSAGE)
    return None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

async def _aliases_by_key(
    company_id: int, key_ids: list[int], db: AsyncConnection,
) -> dict[int, list[str]]:
    if not key_ids:
        return {}
    rows = (await db.execute(
        text("""
            SELECT statuskeyid, aliastext
            FROM   payroll.payrollstatuskeyaliases
            WHERE  companyid = :cid AND statuskeyid = ANY(:ids)
            ORDER  BY statuskeyid, statuskeyaliasid
        """),
        {"cid": company_id, "ids": key_ids},
    )).mappings().all()
    result: dict[int, list[str]] = {}
    for row in rows:
        result.setdefault(row["statuskeyid"], []).append(row["aliastext"])
    return result


async def keys_in_use(key_ids: list[int], db: AsyncConnection) -> set[int]:
    """Keys that a day-entry selection or frozen Status evidence references."""
    if not key_ids:
        return set()
    rows = (await db.execute(
        text("""
            SELECT k.statuskeyid
            FROM   payroll.payrollstatuskeys k
            WHERE  k.statuskeyid = ANY(:ids)
              AND (EXISTS (SELECT 1 FROM payroll.payrollperioddriverdayentrystate e
                           WHERE e.statuskeyid = k.statuskeyid)
                OR EXISTS (SELECT 1 FROM payroll.payrollcalculationsnapshotstatusentries s
                           WHERE s.statuskeyid = k.statuskeyid))
        """),
        {"ids": key_ids},
    )).scalars().all()
    return {int(r) for r in rows}


async def _load_key(
    company_id: int, key_id: int, db: AsyncConnection, *, for_update: bool = False,
):
    row = (await db.execute(
        text(f"""
            SELECT {_STATUS_KEY_COLS}
            FROM   payroll.payrollstatuskeys k
            WHERE  k.statuskeyid = :kid AND k.companyid = :cid
            {"FOR UPDATE" if for_update else ""}
        """),
        {"kid": key_id, "cid": company_id},
    )).mappings().first()
    if row is None:
        raise _coded(404, "STATUS_KEY_NOT_FOUND", f"Status key {key_id} not found.")
    return row


async def _status_key_response(company_id: int, key_id: int, db: AsyncConnection) -> StatusKey:
    row = await _load_key(company_id, key_id, db)
    aliases = (await _aliases_by_key(company_id, [key_id], db)).get(key_id, [])
    return _row_to_status_key(row, aliases, key_id in await keys_in_use([key_id], db))


async def get_status_keys(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    include_inactive: bool = False,
) -> list[StatusKey]:
    """The Company's Statuses, by display order then name. Any user with access to the
    Company can read the catalog (the Day Grid needs it)."""
    await check_branch_access(company_id, user_id, db)
    rows = (await db.execute(
        text(f"""
            SELECT {_STATUS_KEY_COLS}
            FROM   payroll.payrollstatuskeys k
            WHERE  k.companyid = :cid {"" if include_inactive else "AND k.isactive"}
            ORDER  BY k.displayorder, k.normalizedkeyname, k.statuskeyid
        """),
        {"cid": company_id},
    )).mappings().all()
    key_ids = [int(r["statuskeyid"]) for r in rows]
    aliases = await _aliases_by_key(company_id, key_ids, db)
    in_use = await keys_in_use(key_ids, db)
    return [
        _row_to_status_key(r, aliases.get(int(r["statuskeyid"]), []), int(r["statuskeyid"]) in in_use)
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Name and alias collisions
# ---------------------------------------------------------------------------

async def _assert_catalog_unambiguous(
    company_id: int,
    key_id: int | None,
    name_normalized: str,
    aliases: dict[str, str],
    active: bool,
    db: AsyncConnection,
) -> None:
    """Mirror of the database rules, for a clear message before the write.

    An alias never equals its own Status's name. When the Status is active, its name is
    unique among the other active names and does not equal another active Status's alias,
    and each alias equals neither another active Status's name nor another active alias.
    Names and aliases of inactive Statuses never block.
    """
    if name_normalized in aliases:
        raise _coded(422, "STATUS_ALIAS_AMBIGUOUS",
                     "An alias cannot be the same as the name of its own Status.")
    if not active:
        return

    other = (await db.execute(
        text("""
            SELECT keyname FROM payroll.payrollstatuskeys
            WHERE  companyid = :cid AND isactive AND normalizedkeyname = :name
              AND  statuskeyid IS DISTINCT FROM CAST(:kid AS integer)
        """),
        {"cid": company_id, "name": name_normalized, "kid": key_id},
    )).scalar_one_or_none()
    if other is not None:
        raise _coded(422, "STATUS_NAME_NOT_UNIQUE",
                     f"An active Status named '{other}' already exists in this Company.")

    owner = (await db.execute(
        text("""
            SELECT k.keyname
            FROM   payroll.payrollstatuskeyaliases a
            JOIN   payroll.payrollstatuskeys k ON k.statuskeyid = a.statuskeyid
            WHERE  a.companyid = :cid AND a.isactive AND a.normalizedalias = :name
              AND  a.statuskeyid IS DISTINCT FROM CAST(:kid AS integer)
        """),
        {"cid": company_id, "name": name_normalized, "kid": key_id},
    )).scalar_one_or_none()
    if owner is not None:
        raise _coded(422, "STATUS_ALIAS_AMBIGUOUS",
                     f"This name is already an alias of the active Status '{owner}'.")

    if not aliases:
        return
    wanted = sorted(aliases)
    by_name = (await db.execute(
        text("""
            SELECT keyname, normalizedkeyname FROM payroll.payrollstatuskeys
            WHERE  companyid = :cid AND isactive AND normalizedkeyname = ANY(:aliases)
              AND  statuskeyid IS DISTINCT FROM CAST(:kid AS integer)
            ORDER  BY normalizedkeyname LIMIT 1
        """),
        {"cid": company_id, "aliases": wanted, "kid": key_id},
    )).mappings().first()
    if by_name is not None:
        raise _coded(
            422, "STATUS_ALIAS_AMBIGUOUS",
            f"The alias '{aliases[by_name['normalizedkeyname']]}' is the name of the active "
            f"Status '{by_name['keyname']}'.")
    by_alias = (await db.execute(
        text("""
            SELECT a.normalizedalias, k.keyname
            FROM   payroll.payrollstatuskeyaliases a
            JOIN   payroll.payrollstatuskeys k ON k.statuskeyid = a.statuskeyid
            WHERE  a.companyid = :cid AND a.isactive AND a.normalizedalias = ANY(:aliases)
              AND  a.statuskeyid IS DISTINCT FROM CAST(:kid AS integer)
            ORDER  BY a.normalizedalias LIMIT 1
        """),
        {"cid": company_id, "aliases": wanted, "kid": key_id},
    )).mappings().first()
    if by_alias is not None:
        raise _coded(
            422, "STATUS_ALIAS_AMBIGUOUS",
            f"The alias '{aliases[by_alias['normalizedalias']]}' is already an alias of the "
            f"active Status '{by_alias['keyname']}'.")


async def _remove_dropped_aliases(
    key_id: int, keep: dict[str, str], db: AsyncConnection,
) -> dict[str, int]:
    """Delete the aliases no longer wanted; returns the kept ones as normalized -> id."""
    existing = {
        row["normalizedalias"]: int(row["statuskeyaliasid"])
        for row in (await db.execute(
            text("SELECT statuskeyaliasid, normalizedalias FROM payroll.payrollstatuskeyaliases "
                 "WHERE statuskeyid = :kid"),
            {"kid": key_id},
        )).mappings().all()
    }
    drop = [alias_id for normalized, alias_id in existing.items() if normalized not in keep]
    if drop:
        await db.execute(
            text("DELETE FROM payroll.payrollstatuskeyaliases WHERE statuskeyaliasid = ANY(:ids)"),
            {"ids": drop},
        )
    return {n: i for n, i in existing.items() if n in keep}


async def _write_aliases(
    company_id: int, key_id: int, user_id: int, wanted: dict[str, str],
    existing: dict[str, int], is_active: bool, db: AsyncConnection,
) -> None:
    for normalized, alias_text in wanted.items():
        if normalized in existing:
            await db.execute(
                text("UPDATE payroll.payrollstatuskeyaliases SET aliastext = :text "
                     "WHERE statuskeyaliasid = :id AND aliastext <> :text"),
                {"text": alias_text, "id": existing[normalized]},
            )
        else:
            await db.execute(
                text("""
                    INSERT INTO payroll.payrollstatuskeyaliases
                        (companyid, statuskeyid, isactive, aliastext, normalizedalias,
                         createdbyuserid)
                    VALUES (:cid, :kid, :active, :text, :norm, :uid)
                """),
                {"cid": company_id, "kid": key_id, "active": is_active, "text": alias_text,
                 "norm": normalized, "uid": user_id},
            )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _validate_rate_binding(hours: Decimal, column_id: int | None) -> None:
    if column_id is not None and hours <= 0:
        raise _coded(
            422, "STATUS_HOURS_REQUIRED",
            "hours_value must be greater than 0 when status_rate_column_id is set.")


async def create_status_key(
    company_id: int,
    user_id: int,
    data: StatusKeyCreate,
    db: AsyncConnection,
) -> StatusKey:
    """Create a Company Status. The technical code is generated server-side. Requires
    AllCompanyBranches scope."""
    await security.ensure_company_admin(company_id, user_id, db)
    # The Company's default Status rate column exists before any Status needs a column.
    await status_rate_columns.ensure_default_status_rate_column(company_id, db)

    name_normalized = _normalized_name(data.key_name, "key_name")
    aliases = _alias_map(data.aliases)
    _validate_rate_binding(data.hours_value, data.status_rate_column_id)
    if data.status_rate_column_id is not None:
        await status_rate_columns.validate_status_rate_column(
            data.status_rate_column_id, company_id, db)
    await _assert_catalog_unambiguous(
        company_id, None, name_normalized, aliases, data.is_active, db)

    new_id: int | None = None
    generated_code = ""
    for _ in range(_CODE_ATTEMPTS):
        generated_code = _generate_status_key_code()
        taken = (await db.execute(
            text("SELECT 1 FROM payroll.payrollstatuskeys "
                 "WHERE companyid = :cid AND normalizedstatuscode = :code"),
            {"cid": company_id, "code": generated_code},
        )).first()
        if taken is not None:
            continue
        try:
            new_id = (await db.execute(
                text("""
                    INSERT INTO payroll.payrollstatuskeys (
                        companyid, keyname, normalizedkeyname, statuscode, normalizedstatuscode,
                        hoursvalue, isoffreason, deductsfromyearlyallowance, allowancecategory,
                        isactive, displayorder, statusratecolumnid, createdbyuserid
                    ) VALUES (
                        :cid, :kname, :nname, :code, :code,
                        :hours, :off, :deducts, :cat,
                        :active, :order, :src_col_id, :uid
                    )
                    RETURNING statuskeyid
                """),
                {
                    "cid": company_id, "kname": data.key_name.strip(), "nname": name_normalized,
                    "code": generated_code, "hours": data.hours_value,
                    "off": data.is_off_reason, "deducts": data.deducts_from_yearly_allowance,
                    "cat": data.category, "active": data.is_active, "order": data.display_order,
                    "src_col_id": data.status_rate_column_id, "uid": user_id,
                },
            )).scalar_one()
        except DBAPIError as exc:
            coded = _database_conflict(exc)
            if coded is not None:
                raise coded from exc
            raise
        break
    if new_id is None:
        raise _coded(500, "STATUS_CODE_NOT_GENERATED",
                     "Could not generate a unique status code. Please try again.")

    try:
        await _write_aliases(company_id, int(new_id), user_id, aliases, {}, data.is_active, db)
    except DBAPIError as exc:
        coded = _database_conflict(exc)
        if coded is not None:
            raise coded from exc
        raise

    await audit.write_settings_audit(
        db, company_id=company_id, branch_id=None, user_id=user_id,
        action_code="STATUS_KEY_CREATED", entity_name="PayrollStatusKeys",
        entity_id=str(new_id),
        new_value={
            "key_name": data.key_name.strip(), "status_code": generated_code,
            "category": data.category, "aliases": list(aliases.values()),
            "hours_value": str(data.hours_value), "is_off_reason": data.is_off_reason,
            "status_rate_column_id": data.status_rate_column_id,
        },
    )
    return await _status_key_response(company_id, int(new_id), db)


async def update_status_key(
    company_id: int,
    user_id: int,
    key_id: int,
    data: StatusKeyUpdate,
    db: AsyncConnection,
) -> StatusKey:
    """Partially update a Status; only the fields sent change. Requires AllCompanyBranches
    scope. A used Status keeps its hours, off-day semantic, Category and rate column."""
    await security.ensure_company_admin(company_id, user_id, db)
    await status_rate_columns.lock_status_catalog(company_id, db)
    row = await _load_key(company_id, key_id, db, for_update=True)
    sent = data.model_fields_set

    new_name = data.key_name if data.key_name is not None else row["keyname"]
    new_category = data.category if data.category is not None else row["allowancecategory"]
    new_hours = data.hours_value if data.hours_value is not None else row["hoursvalue"]
    new_off = data.is_off_reason if data.is_off_reason is not None else bool(row["isoffreason"])
    new_active = data.is_active if data.is_active is not None else bool(row["isactive"])
    new_order = data.display_order if data.display_order is not None else int(row["displayorder"] or 0)
    new_column = (
        data.status_rate_column_id if "status_rate_column_id" in sent
        else row["statusratecolumnid"]
    )

    name_normalized = (
        _normalized_name(new_name, "key_name") if data.key_name is not None
        else row["normalizedkeyname"]
    )
    current_aliases = await _aliases_by_key(company_id, [key_id], db)
    if data.aliases is not None:
        aliases = _alias_map(data.aliases)
    else:
        aliases = {normalize_status_name(a): a for a in current_aliases.get(key_id, [])}

    semantics_changed = (
        Decimal(str(new_hours)) != Decimal(str(row["hoursvalue"]))
        or new_off != bool(row["isoffreason"])
        or new_category != row["allowancecategory"]
        or new_column != row["statusratecolumnid"]
    )
    if semantics_changed and key_id in await keys_in_use([key_id], db):
        raise _coded(409, "STATUS_KEY_IN_USE", _IN_USE_MESSAGE)

    _validate_rate_binding(new_hours, new_column)
    if new_column is not None and (
        new_column != row["statusratecolumnid"] or (new_active and not row["isactive"])
    ):
        await status_rate_columns.validate_status_rate_column(new_column, company_id, db)
    await _assert_catalog_unambiguous(
        company_id, key_id, name_normalized, aliases, new_active, db)

    existing_aliases = await _remove_dropped_aliases(key_id, aliases, db)
    try:
        await db.execute(
            text("""
                UPDATE payroll.payrollstatuskeys
                SET    keyname = :kname, normalizedkeyname = :nname, allowancecategory = :cat,
                       hoursvalue = :hours, isoffreason = :off, isactive = :active,
                       displayorder = :order, statusratecolumnid = :src_col_id,
                       updatedbyuserid = :uid, updatedatutc = NOW()
                WHERE  statuskeyid = :kid AND companyid = :cid
            """),
            {"kid": key_id, "cid": company_id, "kname": new_name, "nname": name_normalized,
             "cat": new_category, "hours": new_hours, "off": new_off, "active": new_active,
             "order": new_order, "src_col_id": new_column, "uid": user_id},
        )
        await _write_aliases(
            company_id, key_id, user_id, aliases, existing_aliases, new_active, db)
    except DBAPIError as exc:
        coded = _database_conflict(exc)
        if coded is not None:
            raise coded from exc
        raise

    await audit.write_settings_audit(
        db, company_id=company_id, branch_id=None, user_id=user_id,
        action_code="STATUS_KEY_UPDATED", entity_name="PayrollStatusKeys",
        entity_id=str(key_id),
        old_value={
            "key_name": row["keyname"], "category": row["allowancecategory"],
            "aliases": current_aliases.get(key_id, []), "hours_value": str(row["hoursvalue"]),
            "is_off_reason": row["isoffreason"], "is_active": row["isactive"],
            "status_rate_column_id": row["statusratecolumnid"],
        },
        new_value={
            "key_name": new_name, "category": new_category,
            "aliases": list(aliases.values()), "hours_value": str(new_hours),
            "is_off_reason": new_off, "is_active": new_active,
            "status_rate_column_id": new_column,
        },
    )
    return await _status_key_response(company_id, key_id, db)


async def delete_status_key(
    company_id: int,
    user_id: int,
    key_id: int,
    db: AsyncConnection,
) -> StatusKey:
    """Deactivate a Status (idempotent). It stays for the selections and frozen evidence that
    name it; its name and aliases stop blocking others. Requires AllCompanyBranches scope."""
    await security.ensure_company_admin(company_id, user_id, db)
    await status_rate_columns.lock_status_catalog(company_id, db)
    row = await _load_key(company_id, key_id, db, for_update=True)
    if not bool(row["isactive"]):
        return await _status_key_response(company_id, key_id, db)

    await db.execute(
        text("""
            UPDATE payroll.payrollstatuskeys
            SET    isactive = FALSE, updatedbyuserid = :uid, updatedatutc = NOW()
            WHERE  statuskeyid = :kid AND companyid = :cid
        """),
        {"kid": key_id, "cid": company_id, "uid": user_id},
    )
    await audit.write_settings_audit(
        db, company_id=company_id, branch_id=None, user_id=user_id,
        action_code="STATUS_KEY_DEACTIVATED", entity_name="PayrollStatusKeys",
        entity_id=str(key_id),
        old_value={"is_active": True, "status_code": row["statuscode"]},
        new_value={"is_active": False},
    )
    return await _status_key_response(company_id, key_id, db)


# ---------------------------------------------------------------------------
# Input resolution
# ---------------------------------------------------------------------------

_RESOLVE_COLS = """
    k.statuskeyid, k.statuscode, k.keyname, k.isoffreason, k.hoursvalue, k.isactive,
    k.statusratecolumnid, k.allowancecategory
"""


async def resolve_status_input(
    company_id: int,
    text_value: str | None,
    db: AsyncConnection,
    allow_deactivated: bool = False,
    *,
    key_id: int | None = None,
) -> dict | None:
    """Resolve the text typed for a Status to exactly one Status of the Company.

    Blank input resolves to None (the caller clears the Status). Otherwise the normalized
    text is looked up as a primary name, then as an alias, then as the technical code, and
    the first lookup with a match decides; it must match exactly one active Status. A Status
    that has since been deactivated matches only when ``allow_deactivated`` is set, and only
    when no active Status matches the same lookup; ``key_id`` restricts the candidates to one
    Status (the caller re-saving a selection unchanged). Anything else is a 422 with a stable
    code. Returns the Status row (id, code, canonical name, flags, hours, rate column).
    """
    if text_value is None or not text_value.strip():
        return None
    normalized = normalize_status_name(text_value)
    code = text_value.strip().upper()
    restrict = "" if key_id is None else " AND k.statuskeyid = :only_kid"

    lookups = (
        ("SELECT {cols} FROM payroll.payrollstatuskeys k "
         "WHERE k.companyid = :cid AND k.normalizedkeyname = :needle" + restrict,
         normalized),
        ("SELECT {cols} FROM payroll.payrollstatuskeys k "
         "JOIN payroll.payrollstatuskeyaliases a ON a.statuskeyid = k.statuskeyid "
         "WHERE k.companyid = :cid AND a.normalizedalias = :needle" + restrict,
         normalized),
        ("SELECT {cols} FROM payroll.payrollstatuskeys k "
         "WHERE k.companyid = :cid AND k.normalizedstatuscode = :needle" + restrict,
         code),
    )
    for sql, needle in lookups:
        rows = [
            dict(r) for r in (await db.execute(
                text(sql.format(cols=_RESOLVE_COLS)),
                {"cid": company_id, "needle": needle, **({} if key_id is None else {"only_kid": key_id})},
            )).mappings().all()
        ]
        active = [r for r in rows if r["isactive"]]
        candidates = active if active else (rows if allow_deactivated else [])
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise _coded(
                422, "STATUS_INPUT_AMBIGUOUS",
                f"'{text_value.strip()}' matches more than one Status. "
                "Use the name of the Status instead.")
    raise _coded(
        422, "STATUS_NOT_FOUND",
        f"'{text_value.strip()}' is not a valid active Status in this Company.")
