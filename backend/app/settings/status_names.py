"""Status names, aliases and categories: the one normalization and the fixed Category set.

A Status has one canonical primary name and any number of input aliases. Names and aliases
are compared by one normalized form, so "Vacation", "  vacation " and "VACATION" are the same
name. The same function normalizes a name, an alias and the text typed into the Day Grid, so
the three can never disagree. The database stores the normalized form and enforces
uniqueness over it; it never recomputes it.
"""
import unicodedata

# The fixed Status Category set. A Category groups and reports; it never decides pay, hours,
# the off-day semantic or Min/Max. There are no customer-defined categories.
STATUS_CATEGORIES: tuple[str, ...] = (
    "Vacation", "Sick", "Bereavement", "Jury Duty", "Personal", "Holiday", "Unpaid", "Other",
)

# Column width of the stored name, alias and rate-column name (and of their normalized forms).
STATUS_NAME_MAX_LENGTH = 200


def normalize_status_name(value: str) -> str:
    """Unicode NFKC, case-folded, outer whitespace trimmed and inner whitespace collapsed.

    Idempotent: normalizing an already normalized value returns it unchanged. A value with
    nothing but whitespace normalizes to the empty string, which callers reject.
    """
    folded = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", value).casefold())
    return " ".join(folded.split())
