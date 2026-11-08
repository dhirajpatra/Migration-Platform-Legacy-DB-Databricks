"""Deterministic source-type -> Databricks type rules (SKILL §11). Every lossy/ambiguous choice returns a flag;
unknown types are mapped to STRING *and flagged high*, never silently."""
from __future__ import annotations
import re


def _f(code: str, sev: str, detail: str) -> dict:
    return {"code": code, "severity": sev, "detail": detail}


def _dec(p, s, flags, what):
    p, s = (p if p is not None else 38), (s or 0)
    if p > 38:
        flags.append(_f("DECIMAL_PRECISION_GT_38", "high", f"{what} precision {p} exceeds Databricks max 38"))
        p = 38
    return f"DECIMAL({p},{s})"


def map_sqlserver(col: dict) -> tuple[str, list[dict]]:
    t, flags = col["type"].lower(), []
    n = col.get("length")
    simple = {"bit": "BOOLEAN", "smallint": "SMALLINT", "int": "INT", "bigint": "BIGINT", "float": "DOUBLE", "real": "FLOAT",
              "date": "DATE", "binary": "BINARY", "varbinary": "BINARY", "image": "BINARY", "xml": "STRING", "sysname": "STRING",
              "money": "DECIMAL(19,4)", "smallmoney": "DECIMAL(10,4)", "uniqueidentifier": "STRING"}
    if t in simple:
        if t == "uniqueidentifier":
            flags.append(_f("GUID_AS_STRING", "low", "stored as canonical lowercase string; compare case-insensitively"))
        if t == "xml":
            flags.append(_f("XML_AS_STRING", "medium", "XML kept as string; XPath/XQuery logic needs rewrite"))
        return simple[t], flags
    if t == "tinyint":
        flags.append(_f("TINYINT_WIDENED", "low", "SQL Server tinyint is 0-255 (unsigned); Databricks TINYINT is signed -> SMALLINT"))
        return "SMALLINT", flags
    if t in ("decimal", "numeric"):
        return _dec(col.get("precision"), col.get("scale"), flags, t), flags
    if t in ("char", "nchar"):
        flags.append(_f("CHAR_PADDING", "low", "fixed-width CHAR padding is not preserved; trim-compare semantics differ"))
        return "STRING", flags
    if t in ("varchar", "nvarchar"):
        return "STRING", flags
    if t in ("text", "ntext"):
        flags.append(_f("DEPRECATED_LOB_TYPE", "low", f"{t} is deprecated in SQL Server; mapped to STRING"))
        return "STRING", flags
    if t in ("datetime", "smalldatetime", "datetime2"):
        if t == "datetime":
            flags.append(_f("DATETIME_PRECISION", "low", "datetime has ~3.33ms granularity; values load exactly, arithmetic at ms level may differ"))
        flags.append(_f("TIMEZONE_POLICY", "low", "TIMESTAMP is session-timezone-aware in Databricks; decide TZ policy once (UTC recommended)"))
        return "TIMESTAMP", flags
    if t == "datetimeoffset":
        flags.append(_f("OFFSET_DROPPED", "medium", "original UTC offset is not retained in TIMESTAMP; add an offset column if needed"))
        return "TIMESTAMP", flags
    if t == "time":
        flags.append(_f("TIME_AS_STRING", "medium", "no TIME type in Databricks; stored as STRING HH:mm:ss[.fffffff]"))
        return "STRING", flags
    if t in ("rowversion", "timestamp"):
        flags.append(_f("ROWVERSION_NOT_PORTABLE", "medium", "rowversion has no equivalent; replace with a CDC watermark if used for sync"))
        return "BINARY", flags
    if t in ("geography", "geometry"):
        flags.append(_f("SPATIAL_AS_STRING", "high", "spatial type kept as WKT string; spatial functions need redesign"))
        return "STRING", flags
    flags.append(_f("UNMAPPED_TYPE", "high", f"no rule for SQL Server type '{col['type']}'; mapped to STRING - needs a human decision"))
    return "STRING", flags


def map_oracle(col: dict) -> tuple[str, list[dict]]:
    raw = col["type"].upper()
    t, flags = re.sub(r"\(.*\)", "", raw).strip(), []
    p, s = col.get("precision"), col.get("scale")
    if t == "NUMBER" or t in ("NUMERIC", "DECIMAL", "INTEGER", "INT", "SMALLINT"):
        if t in ("INTEGER", "INT", "SMALLINT") or (p is not None and (s or 0) == 0):
            if t == "SMALLINT":
                return "SMALLINT", flags
            if p is None:  # INTEGER maps to NUMBER(38) in Oracle
                return "DECIMAL(38,0)", flags
            return ("INT" if p <= 9 else "BIGINT" if p <= 18 else _dec(p, 0, flags, "NUMBER")), flags
        if p is None and s is None:
            flags.append(_f("NUMBER_NO_PRECISION", "high", "NUMBER without precision/scale: profile the data; defaulted to DECIMAL(38,10)"))
            return "DECIMAL(38,10)", flags
        return _dec(p, s, flags, "NUMBER"), flags
    if t == "DATE":
        flags.append(_f("ORACLE_DATE_HAS_TIME", "medium", "Oracle DATE carries a time part; mapped to TIMESTAMP, not DATE"))
        return "TIMESTAMP", flags
    if t.startswith("TIMESTAMP"):
        if "TIME ZONE" in raw:
            flags.append(_f("OFFSET_DROPPED", "medium", "time-zone/offset not retained in TIMESTAMP; decide TZ policy"))
        return "TIMESTAMP", flags
    if t in ("VARCHAR2", "NVARCHAR2", "VARCHAR", "LONG"):
        flags.append(_f("EMPTY_STRING_IS_NULL", "medium", "Oracle treats '' as NULL; Databricks does not - verify NULL handling in data and code"))
        return "STRING", flags
    if t in ("CHAR", "NCHAR"):
        flags += [_f("CHAR_PADDING", "low", "fixed-width CHAR padding is not preserved"),
                  _f("EMPTY_STRING_IS_NULL", "medium", "Oracle treats '' as NULL; Databricks does not")]
        return "STRING", flags
    if t in ("CLOB", "NCLOB"):
        return "STRING", flags
    if t in ("BLOB", "RAW", "LONG RAW", "BFILE"):
        return "BINARY", flags
    if t in ("FLOAT", "BINARY_DOUBLE", "DOUBLE PRECISION"):
        return "DOUBLE", flags
    if t in ("BINARY_FLOAT", "REAL"):
        return "FLOAT", flags
    if t in ("ROWID", "UROWID"):
        flags.append(_f("ROWID_NOT_PORTABLE", "medium", "ROWID has no equivalent; do not rely on it in translated logic"))
        return "STRING", flags
    if t == "XMLTYPE":
        flags.append(_f("XML_AS_STRING", "medium", "XML kept as string; XPath/XQuery logic needs rewrite"))
        return "STRING", flags
    flags.append(_f("UNMAPPED_TYPE", "high", f"no rule for Oracle type '{col['type']}'; mapped to STRING - needs a human decision"))
    return "STRING", flags


def map_type(engine: str, col: dict) -> tuple[str, list[dict]]:
    return {"sqlserver": map_sqlserver, "oracle": map_oracle}[engine](col)
