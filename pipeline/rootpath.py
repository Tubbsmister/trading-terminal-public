"""
Make the repo-root modules (``polymarket_client``, ``kalshi_client``,
``longshot_model``, ``polymarket_trader``, ``poly_ledger`` ...) importable from
inside the ``pipeline`` package.

The project is a flat directory of scripts with no installable package, so the
root isn't automatically on ``sys.path`` when code runs from within a
subpackage. Importing this module (``from .. import rootpath``) fixes that.
Running ``python -m pipeline.collect`` from the repo root already puts the root
on the path; this just makes imports robust to other entry points too.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
