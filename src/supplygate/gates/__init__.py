"""Independent validation gates that every mapped batch passes before it is loaded.

* ``oracle``: per-row plausibility rules on canonical values.
* ``conservation``: every source record is accounted for exactly once.

Both read the mapping's output and the raw feed, never the mapping spec, so the step
that is being checked cannot change how it is checked.
"""
