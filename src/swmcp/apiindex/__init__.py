"""Index local de l'API SolidWorks (spec §7)."""

from __future__ import annotations

from swmcp.apiindex.indexer import build_index, find_typelibs
from swmcp.apiindex.store import ApiDoc, ApiHit, ApiIndex, ApiParam, IndexWriter

__all__ = ["ApiDoc", "ApiHit", "ApiIndex", "ApiParam", "IndexWriter", "build_index", "find_typelibs"]
