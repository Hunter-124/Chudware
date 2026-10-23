"""GSL text serialization and numeric groundedness checking.

  * `gsl_ser` — the frozen `gsl-ser/v1` canonical serialization of a part's state
    (normative spec: `contracts/gsl_ser.md`), plus slot extraction/enumeration.
  * `groundedness` — the numeric groundedness checker: every numeral in model-produced
    text must trace to a slot in the serialized state. `check_groundedness()` backs the
    `groundedness` gap check in `services/verify/battery.py`.

Design boundary: model-produced text never invents a numeral; numbers are copied from
the serialization and verified post-hoc.
"""
