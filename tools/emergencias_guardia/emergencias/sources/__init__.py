from .datex2 import Datex2Source
from .json_source import JsonSource

SOURCE_TYPES = {"datex2": Datex2Source, "json": JsonSource}

__all__ = ["SOURCE_TYPES", "Datex2Source", "JsonSource"]
