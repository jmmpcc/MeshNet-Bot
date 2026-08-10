from .aemet_cap import AemetCapSource
from .che_rss import CheRssSource
from .datex2 import Datex2Source
from .json_source import JsonSource
from .rss import RssSource
from .firms import FirmsSource

SOURCE_TYPES = {
    "aemet_cap": AemetCapSource,
    "che_rss": CheRssSource,
    "datex2": Datex2Source,
    "json": JsonSource,
    "rss": RssSource,
    "firms": FirmsSource,
}

__all__ = [
    "SOURCE_TYPES",
    "AemetCapSource",
    "CheRssSource",
    "Datex2Source",
    "JsonSource",
    "RssSource",
    "FirmsSource",
]
