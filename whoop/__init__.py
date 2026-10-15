"""Two-source WHOOP data pipeline (4.0 and 5.0/MG).

The two data sources are kept **separate**, each in its own local SQLite file:

* ``whoop.api``  -> ``data/whoop_api.sqlite``  -- official Developer API v2
  (OAuth2, computed metrics: cycles / recoveries / sleeps / workouts).
* ``whoop.ble``  -> ``data/whoop_ble.sqlite``  -- reverse-engineered strap
  protocol (raw sensor samples: hr_samples / raw_packets / capture_sessions).

``whoop.reconcile`` is the *only* place the two are combined, and it opens both
files read-only. See ``whoop/store.py`` for the split schema.
"""

__version__ = "0.1.0"
