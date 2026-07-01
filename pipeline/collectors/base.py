"""Base collector interface."""

from __future__ import annotations

from typing import List

from ..models import Market


class Collector:
    """A venue collector returns a list of normalized ``Market`` objects.

    Implementations should be read-only and resilient: a network error on one
    market must not abort the whole collection. Return whatever was fetched.
    """

    venue: str = "base"

    def collect(self, *, limit: int = 500) -> List[Market]:
        raise NotImplementedError
