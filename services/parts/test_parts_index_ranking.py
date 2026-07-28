"""Tests for the coverage-aware, quorum-relaxed FTS5 ranking in
``services.parts.index`` (accuracy fix for the pure-OR bug where one stray
rare token could fabricate confident junk — e.g. "stepper nema 17" returning
ball bearings because of the lone token "17").

Covers:
  - a query whose terms mostly miss returns no confident junk (a doc
    matching only one rare numeric token never outranks docs matching
    several distinct terms, and is excluded when the limit is small)
  - term_coverage() returns correct (matched, total)
  - AND-first ordering: full-coverage rows precede partial-coverage rows
  - prefix matching finds a part from a truncated final token
  - FTS5 special characters in a query never raise
"""
from services.parts.index import PartsIndex


def _rec(part_id, domain, title, standard="", designation="", **params):
    return {
        "part_id": part_id,
        "domain": domain,
        "title": title,
        "standard": standard,
        "designation": designation,
        "params": params,
    }


def _synthetic_records():
    # Several ball-bearing rows whose designation numbers happen to contain
    # "17" (or overlap other rare-looking numeric tokens) but that have
    # nothing to do with stepper motors — the classic pure-OR trap.
    yield _rec(
        "bearing-6203", "bearings", "Deep Groove Ball Bearing 6203",
        standard="DIN 625", designation="6203", bore="17mm", od="40mm",
    )
    yield _rec(
        "bearing-608", "bearings", "Deep Groove Ball Bearing 608ZZ",
        standard="DIN 625", designation="608ZZ", bore="8mm", od="22mm",
    )
    yield _rec(
        "bearing-6017", "bearings", "Deep Groove Ball Bearing 6017",
        standard="DIN 625", designation="6017", bore="85mm", od="130mm",
    )
    # The actual stepper motor — matches all three concepts in
    # "stepper nema 17": stepper, nema, 17 (via nema17 glue-split).
    yield _rec(
        "stepper-nema17", "motion", "NEMA 17 Stepper Motor 1.8deg",
        designation="NEMA17-42BYGH", holding_torque="0.4Nm",
    )
    # Matches "stepper" and "nema" but not "17" — partial coverage, should
    # still rank ahead of the bearings (which match nothing but a stray
    # numeric coincidence) yet behind the full-coverage NEMA17 motor.
    yield _rec(
        "stepper-nema23", "motion", "NEMA 23 Stepper Motor 2 phase",
        designation="NEMA23-57BYGH", holding_torque="1.9Nm",
    )
    # An o-ring, used for the special-character test.
    yield _rec(
        "oring-2-014", "seals", "O-Ring 2-014 Buna-N",
        standard="AS568", designation="2-014",
    )


class TestCoverageAwareRanking:
    def setup_method(self):
        self.index = PartsIndex()
        self.count = self.index.rebuild(_synthetic_records())
        assert self.count == 6

    # -- (a) no confident junk from a lone rare token ------------------

    def test_stray_numeric_token_does_not_fabricate_junk(self):
        results = self.index.search("stepper nema 17", limit=5)
        part_ids = [r["part_id"] for r in results]
        assert part_ids, "expected at least one result"
        # The full-coverage stepper motor must be first.
        assert part_ids[0] == "stepper-nema17"
        # None of the ball bearings — which only coincidentally contain the
        # digits "17"/"6017" etc. — may outrank it, and with a full-coverage
        # hit present plus a real limit, they should not appear at all in a
        # tight top-1 result.
        top1 = self.index.search("stepper nema 17", limit=1)
        assert [r["part_id"] for r in top1] == ["stepper-nema17"]

    def test_single_stray_token_alone_does_not_match_unrelated_query(self):
        # "17" alone should not resolve to a confident single top hit that
        # masquerades as a stepper-motor match; searching for the full
        # nonsense-heavy query must not surface bearings above real matches.
        results = self.index.search("stepper nema 17", limit=10)
        part_ids = [r["part_id"] for r in results]
        stepper_pos = part_ids.index("stepper-nema17")
        for bearing_id in ("bearing-6203", "bearing-6017"):
            if bearing_id in part_ids:
                assert part_ids.index(bearing_id) > stepper_pos

    # -- (b) term_coverage ------------------------------------------------

    def test_term_coverage_full(self):
        matched, total = self.index.term_coverage("stepper nema 17")
        assert (matched, total) == (3, 3)

    def test_term_coverage_partial(self):
        # "nema" matches (from nema17/nema23 designations), "stepper"
        # matches, "17" matches (via bearing bores/designations too) but a
        # genuinely absent concept must report as unmatched.
        matched, total = self.index.term_coverage("stepper nema flibbertigibbet")
        assert total == 3
        assert matched == 2

    def test_term_coverage_empty_query(self):
        assert self.index.term_coverage("") == (0, 0)
        assert self.index.term_coverage("   ") == (0, 0)

    # -- (c) AND-first ordering -------------------------------------------

    def test_and_first_full_coverage_precedes_partial(self):
        # "stepper nema 17" AND-tier only matches stepper-nema17 (full
        # coverage). "stepper nema" (2 terms) would additionally cover
        # stepper-nema23 (partial: no numeric match) — verify it lands
        # strictly after the full-coverage row when both terms plus a
        # third, harder term are queried together.
        results = self.index.search("stepper nema 17", limit=10)
        part_ids = [r["part_id"] for r in results]
        assert part_ids[0] == "stepper-nema17"
        if "stepper-nema23" in part_ids:
            assert part_ids.index("stepper-nema23") > part_ids.index("stepper-nema17")

    # -- (d) prefix matching ------------------------------------------------

    def test_prefix_match_on_truncated_final_token(self):
        # User mid-typing "nema17" as just "nema1" should still find it via
        # the final-term prefix match.
        results = self.index.search("nema1", limit=5)
        part_ids = [r["part_id"] for r in results]
        assert "stepper-nema17" in part_ids

    def test_prefix_match_stepper_partial(self): 
        results = self.index.search("step", limit=10)
        part_ids = [r["part_id"] for r in results]
        assert "stepper-nema17" in part_ids
        assert "stepper-nema23" in part_ids

    # -- (e) FTS5 special characters never raise ---------------------------

    def test_special_characters_do_not_raise(self):
        queries = [
            '"unterminated quote',
            'stepper*',
            '(stepper AND nema',
            'stepper OR nema',
            'stepper NEAR nema',
            '""',
            '***',
            'o-ring" OR "1=1',
            '"; DROP TABLE parts_fts; --',
        ]
        for q in queries:
            results = self.index.search(q, limit=5)
            assert isinstance(results, list)
            coverage = self.index.term_coverage(q)
            assert isinstance(coverage, tuple) and len(coverage) == 2

    def test_oring_designation_with_dash_and_digits(self):
        results = self.index.search("2-014", limit=5)
        part_ids = [r["part_id"] for r in results]
        assert "oring-2-014" in part_ids
