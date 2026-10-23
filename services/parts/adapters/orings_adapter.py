"""CatalogProvider adapter for standard AS568 O-rings.

Generates the SAE AS568B dash-number series in four common elastomer
compounds. Domain: "seal" — the existing reserved domain (contracts/parts.md
§0; see also services/parts/taxonomy.json "seals_gaskets" / "seals_gaskets.orings"
which already routes to this domain).

Data: the AS568B "000"/"100"/"200"/"300"/"400" dash-number series (nominal
cross-sections 0.070"/0.103"/0.139"/0.210"/0.275"), covering dash -001 through
-467. ID/CS values are the published standard dimensions (SAE AS568B; also
reproduced verbatim, with full agreement across independent distributors, as
Parker's "Series 2-XXX" O-ring size table — e.g. Parker/AS568 "2-014" ==
AS568 dash "-014", ID .489" / CS .070"). These are public engineering facts
(dimensions of a standard part), not copyrighted table text.

No rewrite of any existing surface — this is a new, self-contained local
catalog, following the same generation pattern as bolts_adapter.py.
"""
from __future__ import annotations

from typing import Iterator

_DOMAIN = "seal"

# ---------------------------------------------------------------------------
# AS568B dash-number tables: (dash_number, id_in, cs_in).
# Transcribed from the published AS568B standard dimensional table (nominal
# values; distributor tolerance columns omitted — not needed for search/CAD
# nominal use). Source cross-checked across independent AS568 size charts
# (Parker ORD-5700, Boyd, Applied Fluid Power, Spec Seals / Holland Packing).
# ---------------------------------------------------------------------------

# "000" series — nominal CS 0.070" (dash -004 through -050; -001..-003 are
# undersize specialty CS).
_AS568_000 = (
    (1, 0.029, 0.040), (2, 0.042, 0.050), (3, 0.056, 0.060), (4, 0.070, 0.070),
    (5, 0.101, 0.070), (6, 0.114, 0.070), (7, 0.145, 0.070), (8, 0.176, 0.070),
    (9, 0.208, 0.070), (10, 0.239, 0.070), (11, 0.301, 0.070), (12, 0.364, 0.070),
    (13, 0.426, 0.070), (14, 0.489, 0.070), (15, 0.551, 0.070), (16, 0.614, 0.070),
    (17, 0.676, 0.070), (18, 0.739, 0.070), (19, 0.801, 0.070), (20, 0.864, 0.070),
    (21, 0.926, 0.070), (22, 0.989, 0.070), (23, 1.051, 0.070), (24, 1.114, 0.070),
    (25, 1.176, 0.070), (26, 1.239, 0.070), (27, 1.301, 0.070), (28, 1.364, 0.070),
    (29, 1.489, 0.070), (30, 1.614, 0.070), (31, 1.739, 0.070), (32, 1.864, 0.070),
    (33, 1.989, 0.070), (34, 2.114, 0.070), (35, 2.239, 0.070), (36, 2.364, 0.070),
    (37, 2.489, 0.070), (38, 2.614, 0.070), (39, 2.739, 0.070), (40, 2.864, 0.070),
    (41, 2.989, 0.070), (42, 3.239, 0.070), (43, 3.489, 0.070), (44, 3.739, 0.070),
    (45, 3.989, 0.070), (46, 4.239, 0.070), (47, 4.489, 0.070), (48, 4.739, 0.070),
    (49, 4.989, 0.070), (50, 5.239, 0.070),
)

# "100" series — nominal CS 0.103" (dash -102 through -178).
_AS568_100 = (
    (102, 0.049), (103, 0.081), (104, 0.112), (105, 0.143), (106, 0.174),
    (107, 0.206), (108, 0.237), (109, 0.299), (110, 0.362), (111, 0.424),
    (112, 0.487), (113, 0.549), (114, 0.612), (115, 0.674), (116, 0.737),
    (117, 0.799), (118, 0.862), (119, 0.924), (120, 0.987), (121, 1.049),
    (122, 1.112), (123, 1.174), (124, 1.237), (125, 1.299), (126, 1.362),
    (127, 1.424), (128, 1.487), (129, 1.549), (130, 1.612), (131, 1.674),
    (132, 1.737), (133, 1.799), (134, 1.862), (135, 1.925), (136, 1.987),
    (137, 2.050), (138, 2.112), (139, 2.175), (140, 2.237), (141, 2.300),
    (142, 2.362), (143, 2.425), (144, 2.487), (145, 2.550), (146, 2.612),
    (147, 2.675), (148, 2.737), (149, 2.800), (150, 2.862), (151, 2.987),
    (152, 3.237), (153, 3.487), (154, 3.737), (155, 3.987), (156, 4.237),
    (157, 4.487), (158, 4.737), (159, 4.987), (160, 5.237), (161, 5.487),
    (162, 5.737), (163, 5.987), (164, 6.237), (165, 6.487), (166, 6.737),
    (167, 6.987), (168, 7.237), (169, 7.487), (170, 7.737), (171, 7.987),
    (172, 8.237), (173, 8.487), (174, 8.737), (175, 8.987), (176, 9.237),
    (177, 9.487), (178, 9.737),
)
_CS_100 = 0.103

# "200" series — nominal CS 0.139" (dash -201 through -284).
_AS568_200 = (
    (201, 0.171), (202, 0.234), (203, 0.296), (204, 0.359), (205, 0.421),
    (206, 0.484), (207, 0.546), (208, 0.609), (209, 0.671), (210, 0.734),
    (211, 0.796), (212, 0.859), (213, 0.921), (214, 0.984), (215, 1.046),
    (216, 1.109), (217, 1.171), (218, 1.234), (219, 1.296), (220, 1.359),
    (221, 1.421), (222, 1.484), (223, 1.609), (224, 1.734), (225, 1.859),
    (226, 1.984), (227, 2.109), (228, 2.234), (229, 2.359), (230, 2.484),
    (231, 2.609), (232, 2.734), (233, 2.859), (234, 2.984), (235, 3.109),
    (236, 3.234), (237, 3.359), (238, 3.484), (239, 3.609), (240, 3.734),
    (241, 3.859), (242, 3.984), (243, 4.109), (244, 4.234), (245, 4.359),
    (246, 4.484), (247, 4.609), (248, 4.734), (249, 4.859), (250, 4.984),
    (251, 5.109), (252, 5.234), (253, 5.359), (254, 5.484), (255, 5.609),
    (256, 5.734), (257, 5.859), (258, 5.984), (259, 6.234), (260, 6.484),
    (261, 6.734), (262, 6.984), (263, 7.234), (264, 7.484), (265, 7.734),
    (266, 7.984), (267, 8.234), (268, 8.484), (269, 8.734), (270, 8.984),
    (271, 9.234), (272, 9.484), (273, 9.734), (274, 9.984), (275, 10.484),
    (276, 10.984), (277, 11.484), (278, 11.984), (279, 12.984), (280, 13.984),
    (281, 14.984), (282, 15.955), (283, 16.955), (284, 17.955),
)
_CS_200 = 0.139

# "300" series — nominal CS 0.210" (dash -309 through -390).
_AS568_300 = (
    (309, 0.412), (310, 0.475), (311, 0.537), (312, 0.600), (313, 0.662),
    (314, 0.725), (315, 0.787), (316, 0.850), (317, 0.912), (318, 0.975),
    (319, 1.034), (320, 1.100), (321, 1.162), (322, 1.225), (323, 1.287),
    (324, 1.350), (325, 1.475), (326, 1.600), (327, 1.725), (328, 1.850),
    (329, 1.975), (330, 2.100), (331, 2.225), (332, 2.350), (333, 2.475),
    (334, 2.600), (335, 2.725), (336, 2.850), (337, 2.975), (338, 3.100),
    (339, 3.225), (340, 3.350), (341, 3.475), (342, 3.600), (343, 3.725),
    (344, 3.850), (345, 3.975), (346, 4.100), (347, 4.225), (348, 4.350),
    (349, 4.475), (350, 4.600), (351, 4.725), (352, 4.850), (353, 4.975),
    (354, 5.100), (355, 5.225), (356, 5.350), (357, 5.475), (358, 5.600),
    (359, 5.725), (360, 5.850), (361, 5.975), (362, 6.225), (363, 6.475),
    (364, 6.725), (365, 6.975), (366, 7.225), (367, 7.475), (368, 7.725),
    (369, 7.975), (370, 8.225), (371, 8.475), (372, 8.725), (373, 8.975),
    (374, 9.225), (375, 9.475), (376, 9.725), (377, 9.975), (378, 10.475),
    (379, 10.975), (380, 11.475), (381, 11.975), (382, 12.975), (383, 13.975),
    (384, 14.975), (385, 15.955), (386, 16.955), (387, 17.955), (388, 18.955),
    (389, 19.955), (390, 20.955),
)
_CS_300 = 0.210

# "400" series — nominal CS 0.275" (dash -425 through -467).
_AS568_400 = (
    (425, 4.475), (426, 4.600), (427, 4.725), (428, 4.850), (429, 4.975),
    (430, 5.100), (431, 5.225), (432, 5.350), (433, 5.475), (434, 5.600),
    (435, 5.725), (436, 5.850), (437, 5.975), (438, 6.225), (439, 6.475),
    (440, 6.725), (441, 6.975), (442, 7.225), (443, 7.475), (444, 7.725),
    (445, 7.975), (446, 8.475), (447, 8.975), (448, 9.475), (449, 9.975),
    (450, 10.475), (451, 10.975), (452, 11.475), (453, 11.975), (454, 12.475),
    (455, 12.975), (456, 13.475), (457, 13.975), (458, 14.475), (459, 14.975),
    (460, 15.475), (461, 15.955), (462, 16.455), (463, 16.955), (464, 17.455),
    (465, 17.955), (466, 18.455), (467, 18.955),
)
_CS_400 = 0.275

# (code, label, synonyms, durometer, temp_range_f)
_MATERIALS: tuple = (
    ("nbr", "Buna-N (NBR)", "buna buna-n nitrile nbr", "70A", "-40 to 250"),
    ("fkm", "Viton (FKM)", "viton fkm fluorocarbon fluoroelastomer", "75A", "-15 to 400"),
    ("silicone", "Silicone (VMQ)", "silicone vmq", "70A", "-65 to 450"),
    ("epdm", "EPDM", "epdm ethylene propylene diene", "70A", "-65 to 300"),
)


def _iter_dash_table() -> Iterator[tuple[int, float, float]]:
    for dash, id_in, cs_in in _AS568_000:
        yield dash, id_in, cs_in
    for dash, id_in in _AS568_100:
        yield dash, id_in, _CS_100
    for dash, id_in in _AS568_200:
        yield dash, id_in, _CS_200
    for dash, id_in in _AS568_300:
        yield dash, id_in, _CS_300
    for dash, id_in in _AS568_400:
        yield dash, id_in, _CS_400


def _make_record(dash: int, id_in: float, cs_in: float, material: tuple) -> dict:
    code, label, synonyms, durometer, temp_range = material
    dash_str = f"{dash:03d}"
    od_in = round(id_in + 2 * cs_in, 4)
    title = (
        f"AS568-{dash_str} O-Ring {id_in:.3f}\" ID x {cs_in:.3f}\" CS {label} "
        f"o-ring oring seal 2-{dash_str} -{dash_str} AS568-{dash_str} AS-568 "
        f"AS568B dash {dash_str} {synonyms}"
    )
    params = {
        "standard": "AS568B",
        "dash_number": dash_str,
        "id_in": id_in,
        "cs_in": cs_in,
        "od_in": od_in,
        "id_mm": round(id_in * 25.4, 2),
        "cs_mm": round(cs_in * 25.4, 2),
        "od_mm": round(od_in * 25.4, 2),
        "material": code,
        "material_label": label,
        "durometer": durometer,
        "temp_range_f": temp_range,
    }
    return {
        "part_id": f"seal:AS568-{dash_str}:{code}",
        "domain": _DOMAIN,
        "title": title,
        "standard": "AS568B",
        "designation": f"-{dash_str}",
        "params": params,
        "geometry_ref": None,
        "sourcing": [],
        "provenance": (
            "SAE AS568B standard O-ring dash-number dimensional table (ID/CS "
            "nominal values); cross-checked against independent published AS568 "
            "size charts (Parker ORD-5700 'Series 2-XXX', Boyd, Applied Fluid "
            "Power, Spec Seals). These are public dimensional facts for a "
            "standardized part, not copyrighted table text. See "
            "contracts/licenses.md."
        ),
        "license": "public-domain",
    }


class OringsAdapter:
    """CatalogProvider for AS568B standard O-rings (domain "seal")."""

    domain: str = _DOMAIN
    provider_id: str = "orings"

    def search(self, query: str, *,
               domain: str | None = None,
               filters: dict | None = None) -> list[dict]:
        if domain and domain != _DOMAIN:
            return []
        norm = (query or "").strip().lower()
        recs = list(self.iter_records())
        if not norm:
            return recs
        return [r for r in recs if norm in r["title"].lower()]

    def get(self, part_id: str) -> dict | None:
        if not part_id or not part_id.startswith("seal:AS568-"):
            return None
        rest = part_id[len("seal:"):]
        parts = rest.split(":", 1)
        if len(parts) != 2:
            return None
        std_dash, code = parts
        if not std_dash.startswith("AS568-"):
            return None
        try:
            dash = int(std_dash[len("AS568-"):])
        except ValueError:
            return None
        material = next((m for m in _MATERIALS if m[0] == code), None)
        if material is None:
            return None
        for d, id_in, cs_in in _iter_dash_table():
            if d == dash:
                return _make_record(d, id_in, cs_in, material)
        return None

    def count(self) -> int:
        return sum(1 for _ in _iter_dash_table()) * len(_MATERIALS)

    def iter_records(self) -> Iterator[dict]:
        for dash, id_in, cs_in in _iter_dash_table():
            for material in _MATERIALS:
                yield _make_record(dash, id_in, cs_in, material)
