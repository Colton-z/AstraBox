"""Mongo query-document → predicate translator for the SQLite collection-shim.

The 22 repository bodies build Mongo *query documents* (filters), *update
documents* (``$set``/``$inc``), and small *aggregation pipelines*. This module
turns a query document into a Python predicate evaluated against a stored
document (a plain ``dict``), and applies an update document in-place. Matching is
done **in Python over the decoded JSON doc** rather than pushed into SQL, because
the queries reach into arbitrary nested/dotted paths of a free-form document and
the working sets at this layer are small (one session's journal, one turn's
frames). SQL still does the cheap part — ``collection`` partitioning and ``_id``
lookup — via the indexed columns; everything else is matched here.

Covered query operators::

    equality            {"field": value}                  (incl. dotted "a.b.c")
    $eq / $ne           {"field": {"$ne": v}}
    $in / $nin          {"field": {"$in": [...]}}
    $gt / $gte          {"field": {"$gt": v}}              (str + numeric ordering)
    $lt / $lte          {"field": {"$lt": v}}
    $exists             {"field": {"$exists": False}}
    $type               {"field": {"$type": "string"}}     (only in a partial index)
    $or / $and          {"$or": [ {...}, {...} ]}

Covered update operators::

    $set                {"$set": {"field": v, "a.b": v}}   (dotted paths supported)
    $inc                {"$inc": {"field": n}}

Any operator outside the lists above raises :class:`UnsupportedMongoOperator` at
evaluation time. The repos never use ``$push``/``$pull``/``$regex``/
``$elemMatch``/aggregation-update-pipelines.
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = [
    "UnsupportedMongoOperator",
    "MISSING",
    "compile_filter",
    "matches",
    "apply_update",
    "read_path",
    "set_path",
]


class UnsupportedMongoOperator(RuntimeError):
    """Raised when a query/update uses an operator the SQLite shim does not implement.

    Subclasses ``RuntimeError`` so it is not caught by the repos'
    ``except DuplicateKeyError``/transient handlers, and carries the exact
    operator + field.
    """


class _Missing:
    """Sentinel for "path absent" — distinct from a stored ``None`` value.

    Mongo distinguishes a field that is *absent* from one explicitly set to
    ``null``; ``$exists`` and equality-to-``None`` need that distinction, so reads
    of an absent path return :data:`MISSING` rather than ``None``.
    """

    _instance: "_Missing | None" = None

    def __new__(cls) -> "_Missing":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<MISSING>"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


# Mongo $type aliases used in the tree (only "string" appears, in a partial index).
_TYPE_PREDICATES: dict[Any, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    2: lambda v: isinstance(v, str),
    "bool": lambda v: isinstance(v, bool),
    8: lambda v: isinstance(v, bool),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    16: lambda v: isinstance(v, int) and not isinstance(v, bool),
    18: lambda v: isinstance(v, int) and not isinstance(v, bool),
    "long": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "double": lambda v: isinstance(v, float),
    1: lambda v: isinstance(v, float),
    "object": lambda v: isinstance(v, dict),
    3: lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    4: lambda v: isinstance(v, list),
}

# Recognised value-level operators (the keys that may appear inside
# ``{"field": {"$op": operand}}``). Anything else inside such a dict is an
# unsupported operator and raises.
_VALUE_OPERATORS = frozenset(
    {"$eq", "$ne", "$in", "$nin", "$gt", "$gte", "$lt", "$lte", "$exists", "$type"}
)


def read_path(doc: Any, path: str) -> Any:
    """Read a (possibly dotted) ``path`` from ``doc``; return :data:`MISSING` if absent.

    ``"a.b.c"`` descends nested dicts the way Mongo does. A non-dict encountered
    mid-path means the field is absent for that document.
    """
    if "." not in path:
        if isinstance(doc, dict) and path in doc:
            return doc[path]
        return MISSING
    cur: Any = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return MISSING
        cur = cur[part]
    return cur


def set_path(doc: dict[str, Any], path: str, value: Any) -> None:
    """Set a (possibly dotted) ``path`` into ``doc``, creating intermediate dicts.

    Mirrors Mongo's dotted ``$set`` (``{"$set": {"a.b": 1}}`` creates ``a`` then
    sets ``a.b``). Intermediate non-dicts are replaced with dicts, matching the
    way the repos use dotted ``$set`` only on object-typed parents.
    """
    if "." not in path:
        doc[path] = value
        return
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _cmp_lt(a: Any, b: Any) -> bool:
    """``a < b`` with Mongo-ish tolerance: comparable types compare; else False.

    The repos only ever range-compare like-typed values (ISO timestamp strings
    vs strings, epoch ints vs ints), so a ``TypeError`` from comparing across
    types means "not ordered as requested" → ``False`` (never a silent crash).
    """
    if a is MISSING or b is MISSING:
        return False
    try:
        return bool(a < b)
    except TypeError:
        return False


def _cmp_gt(a: Any, b: Any) -> bool:
    if a is MISSING or b is MISSING:
        return False
    try:
        return bool(a > b)
    except TypeError:
        return False


def _eq(a: Any, b: Any) -> bool:
    """Mongo equality: an absent path never equals a concrete operand.

    (Equality to ``None`` matches only an explicit stored ``null``, not an absent
    field — consistent with Mongo and with the repos that test ``{"owner_id":
    None}`` to mean "explicitly null".)
    """
    if a is MISSING:
        return False
    # bool/int must not cross-match (Mongo treats them distinctly; Python does
    # not — guard so {"active": False} doesn't match a stored 0 and vice versa).
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def _field_eq(actual: Any, operand: Any) -> bool:
    """Mongo field equality, including scalar membership in stored arrays."""
    if _eq(actual, operand):
        return True
    if isinstance(actual, list):
        return any(_eq(item, operand) for item in actual)
    return False


def _match_value_operators(actual: Any, spec: dict[str, Any], field: str) -> bool:
    """Evaluate a ``{"$op": operand, ...}`` spec against ``actual`` (one field)."""
    for op, operand in spec.items():
        if op not in _VALUE_OPERATORS:
            raise UnsupportedMongoOperator(
                f"unsupported query operator {op!r} on field {field!r}; the SQLite "
                f"collection backend implements {sorted(_VALUE_OPERATORS)} only "
                "(no $regex/$elemMatch/$all/$size/$mod/$not at this seam)"
            )
        if op == "$eq":
            if not _field_eq(actual, operand):
                return False
        elif op == "$ne":
            # $ne matches when absent OR not-equal (Mongo semantics).
            if actual is not MISSING and _field_eq(actual, operand):
                return False
        elif op == "$in":
            if actual is MISSING or not any(
                _field_eq(actual, value) for value in operand
            ):
                return False
        elif op == "$nin":
            if actual is not MISSING and any(
                _field_eq(actual, value) for value in operand
            ):
                return False
        elif op == "$gt":
            if not _cmp_gt(actual, operand):
                return False
        elif op == "$gte":
            if not (_eq(actual, operand) or _cmp_gt(actual, operand)):
                return False
        elif op == "$lt":
            if not _cmp_lt(actual, operand):
                return False
        elif op == "$lte":
            if not (_eq(actual, operand) or _cmp_lt(actual, operand)):
                return False
        elif op == "$exists":
            present = actual is not MISSING
            if bool(operand) != present:
                return False
        elif op == "$type":
            predicate = _TYPE_PREDICATES.get(operand)
            if predicate is None:
                raise UnsupportedMongoOperator(
                    f"unsupported $type operand {operand!r} on field {field!r}; "
                    f"known: {sorted(k for k in _TYPE_PREDICATES if isinstance(k, str))}"
                )
            if actual is MISSING or not predicate(actual):
                return False
    return True


def _match_field(doc: dict[str, Any], field: str, condition: Any) -> bool:
    """Match one ``{field: condition}`` clause."""
    actual = read_path(doc, field)
    # A condition dict whose keys are operators ($-prefixed) is an operator spec;
    # a plain dict (no $ keys) is an equality match against an embedded document.
    if isinstance(condition, dict) and condition and all(
        isinstance(k, str) and k.startswith("$") for k in condition
    ):
        return _match_value_operators(actual, condition, field)
    return _field_eq(actual, condition)


def matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
    """Return whether ``doc`` satisfies the Mongo ``query`` document.

    Top-level keys are AND-ed. ``$or``/``$and``/``$nor`` are logical combinators;
    every other top-level key is a field condition. ``$expr`` and other unsupported
    top-level operators raise :class:`UnsupportedMongoOperator`.
    """
    for key, condition in query.items():
        if key == "$or":
            if not any(matches(doc, sub) for sub in condition):
                return False
        elif key == "$and":
            if not all(matches(doc, sub) for sub in condition):
                return False
        elif key == "$nor":
            if any(matches(doc, sub) for sub in condition):
                return False
        elif key.startswith("$"):
            raise UnsupportedMongoOperator(
                f"unsupported top-level query operator {key!r}; the SQLite collection "
                "backend implements $or/$and/$nor plus field conditions only"
            )
        else:
            if not _match_field(doc, key, condition):
                return False
    return True


def compile_filter(query: dict[str, Any] | None) -> Callable[[dict[str, Any]], bool]:
    """Compile a query document into a reusable predicate (validates eagerly-ish).

    Returns ``lambda doc: matches(doc, query)``. An empty/``None`` query matches
    everything (Mongo's ``{}``).
    """
    if not query:
        return lambda _doc: True
    return lambda doc: matches(doc, query)


def apply_update(doc: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Apply a Mongo update document to ``doc`` (returns the same dict, mutated).

    Supports ``$set`` (with dotted paths), ``$inc`` and ``$setOnInsert`` — plus
    the "replacement document" form (an update with no ``$``-prefixed top-level
    keys replaces the doc wholesale, preserving ``_id``). ``$setOnInsert`` on a
    MATCHED document is a no-op by Mongo semantics (its payload applies only on
    the upsert-insert path — see :func:`extract_set_on_insert`). Any other
    update operator raises.
    """
    operator_keys = [k for k in update if isinstance(k, str) and k.startswith("$")]
    if not operator_keys:
        # Replacement-document form: keep _id, swap everything else.
        preserved_id = doc.get("_id")
        doc.clear()
        doc.update(update)
        if preserved_id is not None and "_id" not in doc:
            doc["_id"] = preserved_id
        return doc

    for op in operator_keys:
        payload = update[op]
        if op == "$set":
            if not isinstance(payload, dict):
                raise UnsupportedMongoOperator(f"$set payload must be a document, got {type(payload)}")
            for path, value in payload.items():
                set_path(doc, path, value)
        elif op == "$inc":
            if not isinstance(payload, dict):
                raise UnsupportedMongoOperator(f"$inc payload must be a document, got {type(payload)}")
            for path, delta in payload.items():
                current = read_path(doc, path)
                base = current if isinstance(current, (int, float)) and not isinstance(current, bool) else 0
                set_path(doc, path, base + delta)
        elif op == "$setOnInsert":
            # Mongo semantics: applies ONLY when the update inserts (the upsert
            # path reads it via extract_set_on_insert); on a matched document it
            # is ignored, never an error.
            continue
        else:
            raise UnsupportedMongoOperator(
                f"unsupported update operator {op!r}; the SQLite collection backend "
                "implements $set and $inc only (no $push/$pull/$unset/$addToSet/$min/$max "
                "at this seam — the repos never use them)"
            )
    return doc


def extract_set_on_insert(update: dict[str, Any]) -> dict[str, Any]:
    """Build the document an *upsert* inserts when no row matched.

    Mongo's upsert-insert seeds the new doc from the filter's equality fields
    plus the ``$set``/``$inc`` effects plus the ``$setOnInsert`` payload (which
    applies on exactly this path — :func:`apply_update` skips it for matched
    documents). The shim handles the filter-equality seeding in the collection
    layer (it has the filter); this helper materialises the operator effects
    onto a starting dict. ``$inc`` on a fresh doc starts from 0.
    """
    seed: dict[str, Any] = {}
    apply_update(seed, update)
    on_insert = update.get("$setOnInsert")
    if isinstance(on_insert, dict):
        for path, value in on_insert.items():
            set_path(seed, path, value)
    return seed
