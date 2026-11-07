"""AST pre-processor (milestone 0): code text -> normalized IR.

Hybrid strategy, honestly labelled in `parse_status`:
  * sqlglot parses what it can (views, functions, much of T-SQL).
  * Regex extractors cover procedural bodies sqlglot does not expose (PL/SQL blocks, cursors, dynamic SQL).
  * Everything is resolved against the discovery catalog, so unknown identifiers never become graph edges.
ANTLR T-SQL / PL-SQL grammars are the planned upgrade for fully-structured procedural parsing.
"""
from __future__ import annotations
import logging
import re
import sqlglot
from sqlglot import exp
from migcore.models import SourceObject
from migcore.ast.catalog import Catalog, CALLABLE_KINDS

logging.getLogger("sqlglot").setLevel(logging.ERROR)  # procedural fragments fall back to Command by design

NOISE = {"as", "or", "and", "into", "values", "set", "inserted", "deleted", "dual", "new", "old"}
DIALECT = {"sqlserver": "tsql", "oracle": "oracle"}
IDENT = r"[\[\]\"`\w\.#@$]+"

_TOKENIZE = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.S)

INJECTION = re.compile(
    r"ignore (all |any |the )?(previous|prior|above) (instructions|rules)|disregard (the )?(system|previous)|"
    r"system prompt|you are (now )?(an? )?(ai|assistant|llm|claude)|act as (an? )?(ai|assistant)", re.I)

COMMON = {
    "MERGE": r"\bMERGE\s+(?:INTO\s+)?\w", "TRANSACTION_CTRL": r"\b(BEGIN\s+TRAN\w*|COMMIT|ROLLBACK|SAVEPOINT|SAVE\s+TRAN\w*)\b",
    "PIVOT": r"\b(UN)?PIVOT\b", "RECURSIVE_CTE": r"\bWITH\s+RECURSIVE\b",
}
CONSTRUCTS = {
    "tsql": {
        "CURSOR": r"\bDECLARE\s+@?\w+\s+CURSOR\b", "TEMP_TABLE": r"(?<![\w@])#{1,2}\w+",
        "TABLE_VARIABLE": r"\bDECLARE\s+@\w+\s+TABLE\b", "TRY_CATCH": r"\bBEGIN\s+TRY\b",
        "ROWCOUNT_VAR": r"@@ROWCOUNT\b", "ERROR_VAR": r"@@ERROR\b", "IDENTITY_FN": r"\b(SCOPE_IDENTITY|@@IDENTITY|IDENT_CURRENT)\b",
        "OUTPUT_CLAUSE": r"\bOUTPUT\s+(inserted|deleted)\.", "NOLOCK_HINT": r"\bNOLOCK\b|\bWITH\s*\(\s*(UPDLOCK|HOLDLOCK|ROWLOCK)",
        "CROSS_APPLY": r"\b(CROSS|OUTER)\s+APPLY\b", "FOR_XML": r"\bFOR\s+XML\b", "OPENXML": r"\bOPENXML\b",
        "GOTO": r"\bGOTO\s+\w+", "TOP_N": r"\bTOP\s*\(?\s*\d+", "WHILE_LOOP": r"\bWHILE\b",
        "THROW_RAISERROR": r"\b(THROW|RAISERROR)\b", "INSERTED_DELETED": r"\b(inserted|deleted)\b",
    },
    "oracle": {
        "CURSOR": r"\bCURSOR\s+\w+\s+(IS|\()", "BULK_COLLECT": r"\bBULK\s+COLLECT\b", "FORALL": r"\bFORALL\b",
        "CONNECT_BY": r"\bCONNECT\s+BY\b", "ROWNUM": r"\bROWNUM\b", "OUTER_JOIN_PLUS": r"\(\s*\+\s*\)",
        "DECODE": r"\bDECODE\s*\(", "AUTONOMOUS_TRANSACTION": r"\bAUTONOMOUS_TRANSACTION\b",
        "EXCEPTION_BLOCK": r"\bEXCEPTION\s+WHEN\b|\bWHEN\s+OTHERS\b", "SEQUENCE_NEXTVAL": r"\.\s*(NEXTVAL|CURRVAL)\b",
        "MINUS_OP": r"\bMINUS\b", "FOR_UPDATE": r"\bFOR\s+UPDATE\b", "NEW_OLD_REFS": r":\s*(NEW|OLD)\b",
        "FOR_LOOP": r"\bFOR\s+\w+\s+IN\b", "ROWID": r"\bROWID\b",
    },
}
NONDETERMINISM = {
    "tsql": ["GETDATE", "SYSDATETIME", "NEWID", "RAND", "CURRENT_TIMESTAMP", "GETUTCDATE"],
    "oracle": ["SYSDATE", "SYSTIMESTAMP", "SYS_GUID", "DBMS_RANDOM", "CURRENT_DATE"],
}
SIDE_EFFECTS = {
    "tsql": {"XP_CMDSHELL": r"\bxp_cmdshell\b", "SEND_MAIL": r"\bsp_send_dbmail\b", "OPENROWSET": r"\bOPENROWSET\b|\bOPENQUERY\b",
             "BULK_INSERT": r"\bBULK\s+INSERT\b", "LINKED_SERVER": r"\b\w+\.\w+\.\w+\.\w+\b", "CLR": r"\bEXTERNAL\s+NAME\b"},
    "oracle": {"UTL_FILE": r"\bUTL_FILE\b", "UTL_HTTP": r"\bUTL_HTTP\b", "UTL_SMTP_MAIL": r"\bUTL_(SMTP|MAIL)\b",
               "DBMS_SCHEDULER": r"\bDBMS_(SCHEDULER|JOB)\b", "DB_LINK": r"@\s*\w+\s*(?=[\s;,)])", "DBMS_PIPE_ALERT": r"\bDBMS_(PIPE|ALERT)\b",
               "JAVA_SOURCE": r"\bLANGUAGE\s+JAVA\b"},
}


def _scrub(code: str) -> tuple[str, list[str]]:
    """Blank out comments (kept separately for the injection scan); keep string literals."""
    comments: list[str] = []

    def repl(m: re.Match) -> str:
        t = m.group(0)
        if t.startswith("'"):
            return t
        comments.append(t)
        return " "
    return _TOKENIZE.sub(repl, code), comments


def _norm_ident(raw: str) -> tuple[str | None, str, bool]:
    """-> (schema|None, name, cross_db)"""
    parts = [p.strip('[]"` ') for p in raw.split(".") if p.strip('[]"` ')]
    if not parts:
        return None, "", False
    cross = len(parts) > 2
    name = parts[-1]
    schema = parts[-2] if len(parts) >= 2 else None
    return schema, name, cross


_FROM_END = re.compile(r"\b(WHERE|GROUP|ORDER|HAVING|UNION|INTERSECT|EXCEPT|MINUS|CONNECT|START|ON|SET|VALUES|INTO|JOIN|INNER|LEFT|RIGHT|FULL|CROSS|OUTER|WITH|OPTION|FETCH|FOR|LOOP|BEGIN|END|SELECT)\b|[;)]", re.I)
_SKIP_TOK = {"select", "(", "inserted", "deleted", "dual", "table", "lateral", "xmltable"}


def _read_refs(text: str) -> list[str]:
    refs: list[str] = []
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+", text, re.I):
        tail = text[m.end():]
        e = _FROM_END.search(tail)
        seg = tail[: e.start()] if e else tail[:200]
        for item in seg.split(","):
            tok = item.strip().split()
            if tok and re.fullmatch(IDENT, tok[0]) and tok[0].lower() not in _SKIP_TOK:
                refs.append(tok[0])
    return refs


def _write_refs(text: str) -> list[tuple[str, str]]:
    w: list[tuple[str, str]] = []
    for op, pat in (("INSERT", rf"\bINSERT\s+(?:INTO\s+)?({IDENT})"),
                    ("UPDATE", rf"(?<![\w])UPDATE\s+({IDENT})(?=\s+(?:SET\b|\w+\s+SET\b|AS\s+\w+\s+SET\b))"),
                    ("DELETE", rf"\bDELETE\s+(?:FROM\s+)?({IDENT})"),
                    ("MERGE", rf"\bMERGE\s+INTO\s+({IDENT})"),
                    ("TRUNCATE", rf"\bTRUNCATE\s+TABLE\s+({IDENT})")):
        for m in re.finditer(pat, text, re.I):
            if m.group(1).lower() not in {"into", "values", "select"}:
                w.append((op, m.group(1)))
    return w


def _sqlglot_pass(code: str, dialect: str) -> tuple[str, list[str], list[tuple[str, str]], set[str]]:
    """-> (status, read_refs, write_refs, cte_names)"""
    try:
        trees = [t for t in sqlglot.parse(code, read=dialect) if t is not None]
    except Exception:
        return "error", [], [], set()
    status, reads, writes, ctes = "ok", [], [], set()
    for t in trees:
        if list(t.find_all(exp.Command)):
            status = "command"
        ctes |= {c.alias_or_name.lower() for c in t.find_all(exp.CTE)}
        for tb in t.find_all(exp.Table):
            if tb.name:
                reads.append(".".join(p for p in (tb.db, tb.name) if p))
        for node, op in ((exp.Insert, "INSERT"), (exp.Update, "UPDATE"), (exp.Delete, "DELETE"), (exp.Merge, "MERGE")):
            for n in t.find_all(node):
                tgt = n.this
                tgt = tgt.this if isinstance(tgt, exp.Schema) else tgt
                if isinstance(tgt, exp.Table) and tgt.name:
                    writes.append((op, ".".join(p for p in (tgt.db, tgt.name) if p)))
    return status, reads, writes, ctes


def _dynamic_sql(text: str, dialect: str) -> list[dict]:
    out: list[dict] = []
    lit = re.compile(r"^N?'(?:[^']|'')*'$", re.S)
    if dialect == "tsql":
        for m in re.finditer(r"\bEXEC(?:UTE)?\s*\(\s*(.*?)\s*\)\s*(?:;|\n|$)", text, re.S | re.I):
            out.append({"kind": "EXEC_STRING", "resolved": bool(lit.match(m.group(1).strip())),
                        "line": text[: m.start()].count("\n") + 1})
        for m in re.finditer(r"\bsp_executesql\s+(N?'(?:[^']|'')*'|@\w+)", text, re.I):
            out.append({"kind": "SP_EXECUTESQL", "resolved": m.group(1).upper().lstrip("N").startswith("'"),
                        "line": text[: m.start()].count("\n") + 1})
    else:
        for m in re.finditer(r"\bEXECUTE\s+IMMEDIATE\s+(.+?)(?:\bUSING\b|\bINTO\b|\bRETURNING\b|;)", text, re.S | re.I):
            out.append({"kind": "EXECUTE_IMMEDIATE", "resolved": bool(lit.match(m.group(1).strip())),
                        "line": text[: m.start()].count("\n") + 1})
        if re.search(r"\bDBMS_SQL\b", text, re.I):
            out.append({"kind": "DBMS_SQL", "resolved": False, "line": 0})
    return out


def build_ir(obj: SourceObject, catalog: Catalog) -> dict:
    dialect = DIALECT[obj.engine]
    issues: list[dict] = []
    base = {"object": {"id": obj.fqn, "source_id": obj.source_id, "type": obj.kind, "schema": obj.schema_name,
                       "name": obj.name, "dialect": dialect, "source_hash": obj.source_hash}}
    if obj.code is None:
        issues.append({"code": "CODE_UNAVAILABLE", "severity": "high", "detail": "encrypted or unreadable module; not bypassed"})
        return {**base, "parse_status": "unavailable", "issues": issues, "loc": 0, "tables_read": [], "tables_written": [],
                "calls": [], "constructs": [], "nondeterminism": [], "side_effects": [], "dynamic_sql": [],
                "control_flow": {}, "unresolved_refs": [], "cross_db_refs": []}

    text, comments = _scrub(obj.code)
    if any(INJECTION.search(c) for c in comments) or INJECTION.search(text):
        issues.append({"code": "PROMPT_INJECTION_SUSPECTED", "severity": "high",
                       "detail": "instruction-like text inside comments/strings; quarantined from agent prompts"})

    sg_status, sg_reads, sg_writes, ctes = _sqlglot_pass(obj.code, dialect)
    declared_cursors = ({m.group(1).lower() for m in re.finditer(r"\bDECLARE\s+(\w+)\s+CURSOR\b", text, re.I)} |
                        {m.group(1).lower() for m in re.finditer(r"\bCURSOR\s+(\w+)\s+(?:IS|\()", text, re.I)})
    ctes |= {m.group(1).lower() for m in re.finditer(r"\bWITH\s+(\w+)\s+AS\s*\(", text, re.I)}

    writes_raw = set(_write_refs(text)) | set(sg_writes)
    write_names = {r.lower() for _, r in writes_raw}
    # sqlglot lists INSERT/UPDATE targets as tables; a pure write target is not also a read
    reads_raw = set(_read_refs(text)) | {r for r in sg_reads if r.lower() not in write_names}
    temp_names = {m.group(1).lower() for m in re.finditer(r"#{1,2}(\w+)", text)}
    var_names = {m.group(1).lower() for m in re.finditer(r"@(\w+)", text)}

    tables_read: dict[str, dict] = {}
    tables_written: dict[str, dict] = {}
    unresolved: set[str] = set()
    cross_db: set[str] = set()
    own = (obj.schema_name.lower(), obj.name.lower())

    def resolve(raw: str) -> str | None:
        schema, name, cross = _norm_ident(raw)
        nl = name.lower()
        if (not name or name.startswith(("#", "@")) or nl in ctes or nl in declared_cursors
                or nl in NOISE or nl in temp_names or nl in var_names or nl in catalog.callables):
            return None
        if cross:
            cross_db.add(raw)
            return None
        if (schema or obj.schema_name).lower() == own[0] and name.lower() == own[1] and obj.kind not in ("TABLE",):
            return None
        fqn, why = catalog.resolve_relation(schema, name, obj.schema_name)
        if fqn is None:
            if why == "ambiguous" or (name.lower() not in {"dual"} and len(name) > 1):
                unresolved.add(raw)
        return fqn

    for raw in reads_raw:
        fqn = resolve(raw)
        if fqn:
            tables_read.setdefault(fqn, {"name": fqn})
    for op, raw in writes_raw:
        fqn = resolve(raw)
        if fqn:
            e = tables_written.setdefault(fqn, {"name": fqn, "ops": []})
            if op not in e["ops"]:
                e["ops"].append(op)
    # a trigger "fires on" its table; that relation is carried in meta, not as a read/write
    tokens = set(re.findall(r"[A-Za-z_][\w$#]*", text.lower()))
    calls: list[str] = []
    for name, entries in catalog.callables.items():
        if name in tokens:
            for fqn, _, _ in entries:
                if fqn != obj.fqn and fqn not in calls:
                    calls.append(fqn)

    constructs = [k for k, p in {**COMMON, **CONSTRUCTS[dialect]}.items() if re.search(p, text, re.I)]
    nondet = [f for f in NONDETERMINISM[dialect] if re.search(rf"\b{f}\b", text, re.I)]
    side = [k for k, p in SIDE_EFFECTS[dialect].items() if re.search(p, text, re.I)]
    dyn = _dynamic_sql(text, dialect)
    for d in dyn:
        if not d["resolved"]:
            issues.append({"code": "DYN_SQL_UNRESOLVED", "severity": "high", "detail": f"{d['kind']} line {d['line']}"})
    if "AUTONOMOUS_TRANSACTION" in constructs:
        issues.append({"code": "AUTONOMOUS_TXN", "severity": "high", "detail": "needs redesign + human review"})
    for s in side:
        issues.append({"code": f"SIDE_EFFECT_{s}", "severity": "high", "detail": "external effect; no Databricks equivalent"})

    flow = {
        "cursors": len(re.findall(CONSTRUCTS[dialect]["CURSOR"], text, re.I)),
        "loops": len(re.findall(r"\bWHILE\b|\bFOR\s+\w+\s+IN\b|\bLOOP\b", text, re.I)),
        "branches": len(re.findall(r"\b(IF|ELSIF|ELSEIF|CASE)\b", text, re.I)),
        "try_catch": "TRY_CATCH" in constructs or "EXCEPTION_BLOCK" in constructs,
        "transactions": len(re.findall(COMMON["TRANSACTION_CTRL"], text, re.I)),
    }
    status = "regex_only" if sg_status == "error" else (
        "full" if sg_status == "ok" and obj.kind in ("VIEW", "FUNCTION") and not dyn else "hybrid")

    return {**base, "parse_status": status, "loc": sum(1 for l in text.splitlines() if l.strip()),
            "tables_read": sorted(tables_read), "tables_written": [tables_written[k] for k in sorted(tables_written)],
            "calls": sorted(calls), "constructs": sorted(constructs), "nondeterminism": nondet, "side_effects": side,
            "dynamic_sql": dyn, "control_flow": flow, "unresolved_refs": sorted(unresolved), "cross_db_refs": sorted(cross_db),
            "issues": issues, "meta": obj.meta}
