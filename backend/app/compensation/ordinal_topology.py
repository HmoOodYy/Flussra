"""The OrdinalTier topology a PayDefinition owns.

OrdinalTier structure is definition-owned: an ordered list of tiers, each covering a range of
ordinal positions (the Nth unit of a quantity). A Driver's schedule only supplies a value for
every tier; it never reshapes them. This module is the one validation of that shape, shared by
the governance writers (request submission and approval, direct creation) and by the
calculation and evidence that depend on it. The database repeats it (an overlap or a second
open-ended tier is excluded by constraint, gaps and the open final tier are asserted when the
structure is created and when a schedule is approved).
"""
from collections.abc import Sequence


def topology_error(tiers: Sequence[tuple[int, int | None]]) -> str | None:
    """Why ordered ``(ordinal_from, ordinal_to)`` bounds are not a valid topology, else None.

    Valid means: at least one tier; the first tier starts at 1; bounds are positive integers
    with ``to >= from``; each tier starts right after the previous one ends (no gap, no
    overlap); exactly the last tier is open-ended.
    """
    if not tiers:
        return "At least one tier is required."
    expected_from = 1
    for index, (ordinal_from, ordinal_to) in enumerate(tiers):
        is_last = index == len(tiers) - 1
        if isinstance(ordinal_from, bool) or not isinstance(ordinal_from, int):
            return "Tier bounds must be whole numbers."
        if ordinal_to is not None and (
            isinstance(ordinal_to, bool) or not isinstance(ordinal_to, int)
        ):
            return "Tier bounds must be whole numbers."
        if ordinal_from < 1 or (ordinal_to is not None and ordinal_to < 1):
            return "Tier bounds must be positive."
        if ordinal_from != expected_from:
            return (
                "The first tier must start at 1." if index == 0
                else "Tiers must not leave a gap or overlap: each tier starts right after "
                     "the previous one ends."
            )
        if ordinal_to is None:
            if not is_last:
                return "Only the last tier can be open-ended."
        else:
            if ordinal_to < ordinal_from:
                return "A tier cannot end before it starts."
            if is_last:
                return "The last tier must be open-ended."
            expected_from = ordinal_to + 1
    return None
