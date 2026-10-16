"""Small SQL construction helpers shared by application services."""


def build_in_clause(ids: list[int], prefix: str) -> tuple[str, dict[str, int]]:
    """
    Build a SQL IN clause fragment and matching params dict for sqlalchemy.text().

    Example:
        build_in_clause([1, 2, 3], "b")
        → (":b0, :b1, :b2", {"b0": 1, "b1": 2, "b2": 3})
    """
    keys = [f"{prefix}{i}" for i in range(len(ids))]
    clause = ", ".join(f":{k}" for k in keys)
    params = dict(zip(keys, ids))
    return clause, params
