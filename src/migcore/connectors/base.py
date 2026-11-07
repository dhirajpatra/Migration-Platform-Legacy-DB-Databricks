from __future__ import annotations
from abc import ABC, abstractmethod
from migcore.models import SourceObject


class SourceConnector(ABC):
    """READ-ONLY contract. Implementations must never issue DML/DDL."""
    engine: str

    def __init__(self, source_id: str, cfg: dict):
        self.source_id = source_id
        self.cfg = cfg

    @abstractmethod
    def check_readonly(self) -> list[str]:
        """Return write-capable permissions found (empty list == safe)."""

    @abstractmethod
    def list_tables(self) -> list[dict]:
        """[{schema, name, row_estimate, columns:[{name,type,length,precision,scale,nullable,identity}], pk:[...]}]"""

    @abstractmethod
    def list_foreign_keys(self) -> list[dict]:
        """[{name, schema, table, ref_schema, ref_table}]"""

    @abstractmethod
    def list_code_objects(self) -> list[SourceObject]:
        """Views, procedures, functions, packages, triggers (with code text)."""

    def close(self) -> None:  # pragma: no cover
        pass
