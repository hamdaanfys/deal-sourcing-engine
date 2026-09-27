"""Pluggable data sources. Importing this package registers the built-in sources."""

from dealsource.sources import census_cbp, csv_source  # noqa: F401  (registration side effect)
from dealsource.sources.base import COMPANY_SOURCES, MARKET_SOURCES

__all__ = ["COMPANY_SOURCES", "MARKET_SOURCES"]
