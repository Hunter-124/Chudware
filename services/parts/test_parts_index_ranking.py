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


class TestNarrowestClauseWins:
    """A glued designation must not be satisfiable by its own fragments.

    Measured on the shipped 3182-record index: bare "3" hit 753 records and bare
    "12" hit 318, against 46 for "m3" and 72 for "12mm". Because the coverage tier
    dominates ranking, OR-ing those fragments into the term-group promoted anything
    carrying a stray digit — "M3 socket head cap screw 12mm" ranked a KiCad
    mounting-hole footprint and three shaft collars ABOVE fastener:ISO 4762:M3, and
    "M3x12 SHCS" dropped the size group entirely and answered with the M2 screw.
    """

    def setup_method(self):
        self.index = PartsIndex()
        self.index.rebuild([
            _rec("shcs-m3", "fastener",
                 "Socket Head Cap Screw M3 socket head cap screw shcs",
                 standard="ISO 4762", designation="M3",
                 d=3.0, pitch=0.5, head_d=5.5),
            _rec("shcs-m2", "fastener",
                 "Socket Head Cap Screw M2 socket head cap screw shcs",
                 standard="ISO 4762", designation="M2",
                 d=2.0, pitch=0.4, head_d=3.8),
            # The junk that used to win: a mounting hole whose text carries "M3",
            # "screw" and a bare "mm", i.e. one fragment of every term-group.
            _rec("mountinghole-m3", "ecad",
                 "MountingHole_3.2mm_M3 footprint mounting hole 3.2 mm for M3 screw",
                 designation="MountingHole_3.2mm_M3"),
            # A collar carrying a bare "12" and the word "screw" (set screw).
            _rec("collar-12", "motion",
                 "set-screw shaft collar 12mm bore shaft collar set screw",
                 designation="COLLAR-setscrew-12mm", bore=12.0, od=27.6),
            # No glued spelling in the corpus: the fallback fragments MUST still
            # reach this row from a "608zz" query.
            _rec("bearing-608", "bearing", "Deep-Groove Ball Bearing 608 bearing",
                 standard="ISO 15", designation="608", bore=8.0),
        ])

    def test_exact_family_outranks_fragment_junk(self):
        # The mounting hole's ONLY claim on this query is fragments: "M3", the word
        # "screw", and a bare "mm" split out of "12mm". It used to reach full
        # coverage on that and outrank the screw itself. (The collar is a genuine
        # partial match — it really does carry a whole "12mm" token — so it is not
        # part of this assertion.)
        ids = [r["part_id"] for r in
               self.index.search("M3 socket head cap screw 12mm", limit=5)]
        assert "shcs-m3" in ids, ids
        assert "mountinghole-m3" not in ids[:ids.index("shcs-m3")], ids

    def test_thread_x_length_keeps_the_thread_size(self):
        # "m3x12" is absent from the corpus; the designation head "m3" must still
        # constrain the search, or the top hit is the wrong-size screw.
        for query in ("M3x12 SHCS", "M3 x 12 SHCS"):
            ids = [r["part_id"] for r in self.index.search(query, limit=3)]
            assert ids[0] == "shcs-m3", (query, ids)
            if "shcs-m2" in ids:
                assert ids.index("shcs-m2") > ids.index("shcs-m3"), (query, ids)

    def test_absent_glued_spelling_still_falls_back_to_fragments(self):
        ids = [r["part_id"] for r in self.index.search("608zz bearing", limit=3)]
        assert ids[0] == "bearing-608", ids


class TestAliasesAreSearchable:
    """A standard's alternate designation is how buyers spell it.

    ``aliases`` used to be dropped on the floor by the row builder, so "DIN 912 M3"
    matched no fastener at all and relaxed down to an unrelated row.
    """

    def setup_method(self):
        self.index = PartsIndex()
        shcs = _rec("shcs-m3", "fastener", "Socket Head Cap Screw M3 shcs",
                    standard="ISO 4762", designation="M3", d=3.0)
        shcs["aliases"] = ["DIN 912", "SHCS"]
        oring = _rec("oring-317", "seal",
                     'AS568-317 O-Ring 0.912" ID x 0.210" CS Silicone oring',
                     standard="AS568", designation="-317")
        self.index.rebuild([shcs, oring])

    def test_din_alias_finds_the_iso_standard(self):
        ids = [r["part_id"] for r in self.index.search("DIN 912 M3", limit=3)]
        assert ids[0] == "shcs-m3", ids
