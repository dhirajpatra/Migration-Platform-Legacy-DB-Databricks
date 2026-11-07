from __future__ import annotations
import hashlib
from typing import Any, Literal
from pydantic import BaseModel, Field

ObjectKind = Literal["TABLE", "VIEW", "PROCEDURE", "FUNCTION", "TRIGGER", "PACKAGE", "PACKAGE_BODY"]


class SourceObject(BaseModel):
    """One extracted database object (code objects carry `code`)."""
    source_id: str
    engine: str
    kind: ObjectKind
    schema_name: str
    name: str
    code: str | None = None          # None => encrypted/unreadable (flagged, never bypassed)
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def fqn(self) -> str:
        return f"{self.source_id}.{self.schema_name}.{self.name}".lower()

    @property
    def source_hash(self) -> str:
        return "sha256:" + hashlib.sha256((self.code or "").encode()).hexdigest()


class Issue(BaseModel):
    code: str
    severity: Literal["low", "medium", "high", "critical"]
    object_id: str | None = None
    detail: str = ""
