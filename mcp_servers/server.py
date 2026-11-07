"""Read-only MCP connector server. One image, ENGINE=sqlserver|oracle selects the connector.
Agents may later use these tools over MCP instead of the in-process connector (same contract)."""
from __future__ import annotations
import os
from mcp.server.fastmcp import FastMCP
from migcore.connectors.registry import build_connector

ENGINE = os.environ["ENGINE"]


def _src() -> dict:
    if ENGINE == "sqlserver":
        return {"id": "mssql", "engine": "sqlserver", "host": os.environ["MSSQL_HOST"], "port": os.environ.get("MSSQL_PORT", "1433"),
                "database": os.environ["MSSQL_DATABASE"], "user": os.environ["MSSQL_USER"], "password": os.environ["MSSQL_PASSWORD"]}
    return {"id": "oracle", "engine": "oracle", "host": os.environ["ORACLE_HOST"], "port": os.environ.get("ORACLE_PORT", "1521"),
            "service": os.environ["ORACLE_SERVICE"], "user": os.environ["ORACLE_USER"], "password": os.environ["ORACLE_PASSWORD"],
            "schemas": [s for s in os.environ.get("ORACLE_SCHEMAS", "DEMO").split(",") if s]}


mcp = FastMCP(f"mcp-{ENGINE}", host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "8101")))


def _with_conn(fn):
    conn = build_connector(_src())
    try:
        return fn(conn)
    finally:
        conn.close()


@mcp.tool()
def check_readonly() -> list[str]:
    """Write-capable permissions held by the connector login. Must be empty."""
    return _with_conn(lambda c: c.check_readonly())


@mcp.tool()
def list_tables() -> list[dict]:
    """Tables with columns, primary keys and row estimates."""
    return _with_conn(lambda c: c.list_tables())


@mcp.tool()
def list_foreign_keys() -> list[dict]:
    """Foreign-key relationships."""
    return _with_conn(lambda c: c.list_foreign_keys())


@mcp.tool()
def list_code_objects() -> list[dict]:
    """Views, procedures, functions, packages and triggers with their source text."""
    return _with_conn(lambda c: [o.model_dump() for o in c.list_code_objects()])


@mcp.tool()
def get_ddl(schema: str, name: str) -> str:
    """Source text of one code object."""
    for o in _with_conn(lambda c: c.list_code_objects()):
        if o.schema_name.lower() == schema.lower() and o.name.lower() == name.lower():
            return o.code or "-- unavailable (encrypted/unreadable)"
    return "-- not found"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
