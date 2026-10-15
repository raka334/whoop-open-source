"""Unified WHOOP 5.0 data pipeline.

Two independent data sources feed a single local SQLite store:

* ``whoop.api``  - the official WHOOP Developer API (v2), OAuth2, processed metrics.
* ``whoop.ble``  - the reverse-engineered strap protocol, raw sensor samples.

The store (``whoop.store``) is the shared seam between them.
"""

__version__ = "0.1.0"
