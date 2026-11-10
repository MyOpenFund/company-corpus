"""Facts as filed -> one value per concept and period (chantier 9, spec §7).

One SQL statement generated from the dictionary, run by the my-open-data reader as a ``Recipe``
and by the F1 engine in a local DuckDB connection. The input ``facts`` holds every vintage knowable
at the read date. Only the latest row per tag and period is a candidate; the one it replaced serves
to flag a strong restatement.

For one concept and period, candidates rank by: on the concept's statement, on another statement,
elsewhere; then latest filing; then dictionary order. A filing without position rows borrows the
positions of the issuer's latest earlier filing that has some (``location = inferred``). Positions
are read at each run: once the real ones arrive, the value is ``statement`` or ``note``, with no
change to any stored fact. ``on_statement`` says the value's line is on the concept's statement, in
the filing's own positions or the borrowed ones.

Several taxonomies (one tag table each) share the selection. A position places a fact only when its
``tag_version`` starts with the fact's table prefix. For one concept and period, only the taxonomy of
the latest filing with a candidate answers (on the same filing: the one on the statement, then table
order), so a filer that changed taxonomy gets one row, never one per taxonomy. A sum concept's
candidates are its statement lines (only those can be summed), so only they decide which taxonomy
answers. A computed total reads the base concept in its own table's taxonomy only.

A fact filed before its period ended (its ``known_at``, read as a date in the filing zone, before
its ``valid_date``) is dropped for a concept whose ``filed_before_period_end`` rule is ``drop``,
computed totals included, and kept for a ``keep`` concept (a dividend declared ahead of its
quarter). The filing zone is EDGAR's (``DEFAULT_FILING_TZ``), a parameter; a filing made on the
period's last day is on time. Known limit: ``facts_in`` and the restatement flag work per tag,
before the concept is known, so an early vintage can still be the ``replaced`` value of a later one.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .dictionary import Dictionary, load_dictionary

RECIPE_NAME = "fundamentals_concepts"
#: EDGAR's zone. A fact is filed before its period ended when its known_at, read as a date in
#: this zone, is before its valid_date: the boundary of F1's publication date (a filing made on
#: the period's last day counts as on time).
DEFAULT_FILING_TZ = "America/New_York"

_PERIOD = "entity_scheme, entity_value, valid_date, frequency, basis, source_code"
_OUT = ("concept, entity_scheme, entity_value, valid_date, frequency, basis, source_code, "
        "period_start, value, unit, original_tag, filing_ref, source_doc_id, known_at, location, "
        "conflict, review_status, on_statement")


def _lit(v) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _values(rows) -> str:
    return ", ".join("(" + ", ".join(_lit(x) for x in r) + ")" for r in rows)


def _zone(filing_tz: str) -> str:
    try:
        ZoneInfo(filing_tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"filing_tz: unknown time zone {filing_tz!r}") from exc
    return filing_tz


def _early(alias: str, filing_tz: str) -> str:
    """SQL: the fact was filed before its period ended. ``known_at`` is a TIMESTAMPTZ on both
    paths (F1: 23:59:59 UTC on the filing date; store: the acceptance instant), and the zone is
    explicit so the session ``TimeZone`` never matters (DuckDB ICU)."""
    return f"timezone({_lit(filing_tz)}, {alias}.known_at)::DATE < {alias}.valid_date"


def _on_position(prefix: str) -> str:
    return ("JOIN pos p ON p.entity_scheme = f.entity_scheme AND p.entity_value = f.entity_value "
            "AND p.filing_ref = f.filing_ref AND p.tag = split_part(f.original_tag, ':', 2) "
            f"AND p.version_prefix = {_lit(prefix)}")


def _computed_ctes(i: int, c, e, prefix: str, namespace: str,
                   filing_tz: str) -> tuple[str, str]:
    m, statement, on_position = e.computed, c.statement, _on_position(prefix)
    # total_i drops early facts of a ``drop`` concept. minus_i repeats the filter as a redundant
    # defence only: it joins total_i on the period and filing_ref, and one filing's facts share
    # known_at and valid_date, so an early minus line can only meet an early (already dropped)
    # total. Kept so the CTE stays correct on its own if the join ever changes.
    on_time = "" if c.filed_before_period_end == "keep" else f" AND NOT {_early('f', filing_tz)}"
    first = _values((k, n) for n, k in enumerate(m.minus_first_of))
    minus = _values((t, n) for n, t in enumerate(m.minus_first_tag_of))
    cte = f""",
total_{i} AS (
  SELECT f.* FROM facts_in f {on_position}
  WHERE f.original_tag = {_lit(m.total_tag)} AND list_contains(p.statements, {_lit(statement)}){on_time}
),
base_{i} AS (
  SELECT {_PERIOD}, filing_ref, arg_min(value, r.n) AS value
  FROM picked JOIN (VALUES {first}) r(concept, n) USING (concept)
  WHERE location = 'statement' AND taxonomy = {_lit(namespace)} GROUP BY {_PERIOD}, filing_ref
),
minus_{i} AS (
  SELECT f.entity_scheme, f.entity_value, f.valid_date, f.frequency, f.basis, f.source_code,
         f.filing_ref, arg_min(f.value, r.n) AS value
  FROM facts_in f JOIN (VALUES {minus}) r(original_tag, n) USING (original_tag) {on_position}
  WHERE list_contains(p.statements, {_lit(statement)}){on_time}
  GROUP BY f.entity_scheme, f.entity_value, f.valid_date, f.frequency, f.basis, f.source_code,
           f.filing_ref
),
computed_{i} AS (
  SELECT {_lit(c.key)} AS concept, t.entity_scheme, t.entity_value, t.valid_date, t.frequency,
         t.basis, t.source_code, t.period_start, t.value - b.value - coalesce(m.value, 0) AS value,
         t.unit, 'computed:' || t.original_tag AS original_tag, t.filing_ref, t.source_doc_id,
         t.known_at, 'derived' AS location, FALSE AS conflict, {_lit(e.status)} AS review_status,
         TRUE AS on_statement
  FROM total_{i} t
  JOIN base_{i} b USING ({_PERIOD}, filing_ref)
  LEFT JOIN minus_{i} m USING ({_PERIOD}, filing_ref)
  ANTI JOIN picked x ON x.concept = {_lit(c.key)} AND x.entity_scheme = t.entity_scheme
       AND x.entity_value = t.entity_value AND x.valid_date = t.valid_date
       AND x.frequency = t.frequency AND x.basis = t.basis AND x.source_code = t.source_code
)"""
    return cte, f"SELECT {_OUT} FROM computed_{i}"


def selection_sql(dictionary: Dictionary | None = None, *, facts: str = "facts",
                  positions: str = "positions", filing_tz: str = DEFAULT_FILING_TZ) -> str:
    """One DuckDB statement over the views ``facts`` and ``positions``; ``filing_tz`` is the zone
    a filing's ``known_at`` is read in to tell whether it came before its period ended.

    ``facts.known_at`` must be a TIMESTAMPTZ: the zone is then explicit and the session
    ``TimeZone`` never matters. A naive TIMESTAMP would be cast through the session zone."""
    filing_tz = _zone(filing_tz)
    d = dictionary or load_dictionary()
    tags = []
    for rank_t, t in enumerate(d.tables):
        for e in t.entries.values():
            chain = [(x, False) for x in e.tags] + [(x, True) for x in e.statement_only_tags]
            tags.extend((e.concept, x, rank, only, e.on_several, e.status, t.namespace, rank_t,
                         t.position_version_prefix)
                        for rank, (x, only) in enumerate(chain))
    concepts = [(c.key, c.statement, c.filed_before_period_end == "keep")
                for c in d.concepts.values()]
    extra, selects = [], [f"SELECT {_OUT} FROM picked", f"SELECT {_OUT} FROM summed"]
    computed = [(t, e) for t in d.tables for e in t.entries.values() if e.computed]
    for i, (t, e) in enumerate(computed):
        cte, select = _computed_ctes(i, d.concepts[e.concept], e, t.position_version_prefix,
                                   t.namespace, filing_tz)
        extra.append(cte)
        selects.append(select)
    tol, restated = repr(d.conflict_tolerance), repr(d.restatement_tolerance)
    key = "entity_scheme, entity_value, original_tag, valid_date, frequency, basis, source_code"
    return f"""WITH dict_tags(concept, original_tag, tag_rank, statement_only, on_several, review_status,
               taxonomy, taxonomy_rank, version_prefix) AS (VALUES {_values(tags)}),
dict_concepts(concept, statement, keep_early) AS (VALUES {_values(concepts)}),
vintages AS (
  SELECT entity_scheme, entity_value, original_tag, period_start, frequency, basis, value, unit,
         filing_ref, source_doc_id, known_at, valid_date, source_code,
         lag(value) OVER (PARTITION BY {key} ORDER BY known_at, filing_ref, value) AS replaced
  FROM {facts}
),
facts_in AS (
  SELECT * FROM vintages
  QUALIFY row_number() OVER (PARTITION BY {key} ORDER BY known_at DESC, filing_ref DESC, value DESC) = 1
),
pos AS (SELECT s.entity_scheme, s.entity_value, s.filing_ref, s.tag, d.version_prefix,
               list(DISTINCT s.statement) AS statements
        FROM {positions} s
        JOIN (SELECT DISTINCT version_prefix FROM dict_tags) d
          ON starts_with(coalesce(s.tag_version, ''), d.version_prefix)
        GROUP BY 1, 2, 3, 4, 5),
located_filings AS (SELECT DISTINCT entity_scheme, entity_value, filing_ref, known_at FROM {positions}),
blind_filings AS (
  SELECT DISTINCT f.entity_scheme, f.entity_value, f.filing_ref, f.known_at FROM facts_in f
  ANTI JOIN located_filings k ON k.entity_scheme = f.entity_scheme
       AND k.entity_value = f.entity_value AND k.filing_ref = f.filing_ref
),
borrowed AS (
  SELECT b.entity_scheme, b.entity_value, b.filing_ref, k.filing_ref AS from_filing FROM blind_filings b
  ASOF JOIN located_filings k ON b.entity_scheme = k.entity_scheme
       AND b.entity_value = k.entity_value AND b.known_at >= k.known_at
),
placed AS (
  SELECT f.entity_scheme, f.entity_value, f.valid_date, f.frequency, f.basis, f.source_code,
         f.period_start, f.value, f.unit, f.original_tag, f.filing_ref, f.source_doc_id, f.known_at,
         f.replaced, t.concept, t.tag_rank, t.statement_only, t.on_several, t.review_status,
         t.taxonomy, t.taxonomy_rank, c.keep_early, {_early('f', filing_tz)} AS early,
         CASE WHEN k.filing_ref IS NOT NULL
              THEN (CASE WHEN p.statements IS NULL THEN 'note' ELSE 'statement' END)
              WHEN b.from_filing IS NOT NULL THEN 'inferred' ELSE 'unknown' END AS location,
         CASE WHEN list_contains(p.statements, c.statement) THEN 0
              WHEN p.statements IS NOT NULL THEN 1 ELSE 2 END AS tier
  FROM facts_in f
  JOIN dict_tags t USING (original_tag)
  JOIN dict_concepts c USING (concept)
  LEFT JOIN (SELECT DISTINCT entity_scheme, entity_value, filing_ref FROM located_filings) k
       ON k.entity_scheme = f.entity_scheme AND k.entity_value = f.entity_value
       AND k.filing_ref = f.filing_ref
  LEFT JOIN borrowed b ON b.entity_scheme = f.entity_scheme AND b.entity_value = f.entity_value
       AND b.filing_ref = f.filing_ref
  LEFT JOIN pos p ON p.entity_scheme = f.entity_scheme AND p.entity_value = f.entity_value
       AND p.filing_ref = coalesce(b.from_filing, f.filing_ref)
       AND p.tag = split_part(f.original_tag, ':', 2)
       AND p.version_prefix = t.version_prefix
),
eligible AS (SELECT * FROM placed WHERE (keep_early OR NOT early)
                  AND CASE WHEN on_several = 'sum' THEN tier = 0
                  ELSE NOT statement_only OR tier = 0 END),
cands AS (
  SELECT * EXCLUDE (answering) FROM (
    SELECT *, first_value(taxonomy) OVER (
             PARTITION BY concept, {_PERIOD}
             ORDER BY known_at DESC, filing_ref DESC, tier, taxonomy_rank) AS answering
    FROM eligible)
  WHERE taxonomy = answering
),
ranked AS (
  SELECT *, row_number() OVER w AS rn, first_value(value) OVER w AS best
  FROM cands WHERE on_several = 'first'
  WINDOW w AS (PARTITION BY concept, {_PERIOD}
               ORDER BY tier, known_at DESC, filing_ref DESC, tag_rank)
),
flagged AS (
  SELECT *, bool_or(CASE WHEN best = 0 THEN value <> 0
                         ELSE abs(value::DOUBLE - best::DOUBLE)
                              > {tol}::DOUBLE * abs(best::DOUBLE) END)
              OVER (PARTITION BY concept, {_PERIOD}) AS conflict
  FROM ranked
),
picked AS (
  SELECT * REPLACE (conflict OR coalesce(CASE WHEN replaced = 0 THEN value <> 0
                    ELSE abs(value::DOUBLE - replaced::DOUBLE)
                              > {restated}::DOUBLE * abs(replaced::DOUBLE) END, FALSE) AS conflict),
         tier = 0 AS on_statement
  FROM flagged WHERE rn = 1
),
summed AS (
  SELECT concept, {_PERIOD}, any_value(period_start) AS period_start, sum(value) AS value,
         any_value(unit) AS unit, string_agg(original_tag, '+' ORDER BY tag_rank) AS original_tag,
         filing_ref, any_value(source_doc_id) AS source_doc_id, known_at, 'derived' AS location,
         FALSE AS conflict, any_value(review_status) AS review_status, TRUE AS on_statement
  FROM (SELECT *, dense_rank() OVER (PARTITION BY concept, {_PERIOD}
                                     ORDER BY known_at DESC, filing_ref DESC) AS filing_rank
        FROM cands WHERE on_several = 'sum' AND tier = 0)
  WHERE filing_rank = 1
  GROUP BY concept, {_PERIOD}, filing_ref, known_at
){''.join(extra)}
SELECT *, {_lit(d.version)} AS dictionary_version FROM ({' UNION ALL '.join(selects)})"""


def recipe(dictionary: Dictionary | None = None, *, filing_tz: str = DEFAULT_FILING_TZ):
    """The ``fundamentals_concepts`` Recipe; needs my-open-data's reader.

    ``filing_tz`` changes which facts count as filed before their period ended, hence the rows,
    but not the Recipe's version label (the dictionary version): a non-default zone gives
    different rows under the same label, so readers should keep the default. The view's
    ``known_at`` must be a TIMESTAMPTZ (my-open-data declares it so), see :func:`selection_sql`."""
    from myopendata.reader import Recipe

    d = dictionary or load_dictionary()
    return Recipe(RECIPE_NAME, d.version, {"facts": "fundamentals:vintages", "positions": "filing_statements"},
                  selection_sql(d, filing_tz=filing_tz))


def run_selection(facts, positions, dictionary: Dictionary | None = None, *,
                  filing_tz: str = DEFAULT_FILING_TZ) -> list[dict]:
    """The Recipe's SQL on in-memory Arrow tables. ``facts.known_at`` must be a timestamp with a
    time zone (TIMESTAMPTZ): a naive one is refused, since reading it in the filing zone would
    depend on the DuckDB session ``TimeZone``."""
    import duckdb
    import pyarrow as pa

    known_at = facts.schema.field("known_at").type
    if pa.types.is_timestamp(known_at) and known_at.tz is None:
        raise ValueError("facts.known_at is a timestamp without time zone: the selection needs "
                         "a TIMESTAMPTZ (a UTC instant)")
    con = duckdb.connect(":memory:")
    try:
        con.register("facts", facts)
        con.register("positions", positions)
        return con.sql(selection_sql(dictionary, filing_tz=filing_tz)).to_arrow_table().to_pylist()
    finally:
        con.close()
