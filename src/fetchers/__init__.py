"""Registry mapping a source `type` (from sources.yaml) to its fetch function.

To support a new kind of source, write a module exposing
`fetch(source: dict, settings: dict) -> list[Paper]` and register it here.
"""

from src.fetchers import crossref, rss

FETCHERS = {
    "rss": rss.fetch,
    "crossref": crossref.fetch,
}
