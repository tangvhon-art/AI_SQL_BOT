"""只读 SQL 执行器：sqlglot 语法/只读校验 → 按方言执行 → 返回行。"""
import logging
import time
from typing import Any

import sqlglot
import sqlglot.expressions as exp
from sqlalchemy import create_engine, text

from .config_override import get_effective as get_settings
from .engine.permission import rewrite_sql_for_user

logger = logging.getLogger(__name__)


class SqlExecError(Exception):
    pass


_DIALECT_MAP = {
    "mysql": "mysql", "postgresql": "postgres", "clickhouse": "clickhouse",
    "sqlserver": "tsql", "oracle": "oracle", "duckdb": "duckdb",
}
_WRITE_KINDS = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Alter,
                exp.Create, exp.Grant, exp.Revoke, exp.Merge, exp.TruncateTable)


def validate_sql(sql: str, dialect: str = "mysql") -> sqlglot.expressions.Select:
    """语法 + 只读校验；返回解析后的 AST。"""
    if not sql or not sql.strip():
        raise SqlExecError("SQL 为空")
    try:
        ast = sqlglot.parse_one(sql, read=_DIALECT_MAP.get(dialect, dialect))
    except Exception as exc:  # noqa: BLE001
        raise SqlExecError(f"SQL 语法错误: {exc}") from exc
    if not isinstance(ast, exp.Query):  # Select / Union 等集合查询均属 Query
        raise SqlExecError("仅允许 SELECT 查询")
    for node in ast.walk():
        if isinstance(node, _WRITE_KINDS):
            raise SqlExecError("仅允许只读 SELECT 查询")
    return ast


def _apply_limit(sql: str, dialect: str, max_rows: int) -> str:
    try:
        ast = sqlglot.parse_one(sql, read=_DIALECT_MAP.get(dialect, dialect))
    except Exception:  # noqa: BLE001
        return sql
    if ast.args.get("limit") is None:
        ast = ast.limit(max_rows)
    return ast.sql(dialect=_DIALECT_MAP.get(dialect, dialect), pretty=True)


def run_query(datasource_id: int, sql: str, user_id: int,
              dialect: str = "mysql") -> dict:
    """完整链路：语法校验 → 字段/行级权限改写 → 成本门槛 → 限流/超时 → 只读执行。
    返回结构见 chat 事件契约。"""
    from .models import Datasource
    from .database import SessionLocal
    from .security import aes_decrypt
    from .engine.rate_limiter import get_limiter, RateLimitExceeded

    settings = get_settings()
    start = time.time()
    ast = validate_sql(sql, dialect)
    rewritten, injected, denied, row_rule_summary = rewrite_sql_for_user(ast, dialect, user_id)
    if denied and not injected:
        # 无任何可查字段可用
        raise SqlExecError("当前权限不允许查询所涉及的字段")

    # C2 · Explain 成本门槛（deny 拦截；warn 放行并标记）
    cost_estimate = None
    from .engine.cost_guard import guard_sql, format_estimate
    db0 = SessionLocal()
    try:
        ds0 = db0.query(Datasource).get(datasource_id)
        if not ds0:
            raise SqlExecError("数据源不存在")
        est = guard_sql(rewritten, dialect, ds0)
        if est is not None:
            cost_estimate = format_estimate(est)
            if est.level == "deny":
                raise SqlExecError(
                    f"查询成本过高已拦截（{est.metric}={est.value:,.0f}，"
                    f"硬阈值 {est.hard:,.0f}），请缩小查询范围")
    finally:
        db0.close()

    final_sql = _apply_limit(rewritten, dialect, settings.query_max_rows)

    # C6 · 限流与并发闸（排队超时抛 RateLimitExceeded）
    limiter = get_limiter()
    try:
        limiter.acquire(user_id)
    except RateLimitExceeded:
        raise
    try:
        db = SessionLocal()
        try:
            ds = db.query(Datasource).get(datasource_id)
            if not ds:
                raise SqlExecError("数据源不存在")
            from urllib.parse import quote_plus
            scheme = "postgresql+psycopg2" if ds.type == "postgresql" else "mysql+pymysql"
            url = (
                f"{scheme}://{quote_plus(ds.user)}:{quote_plus(aes_decrypt(ds.password_enc))}"
                f"@{ds.host}:{ds.port}/{ds.db_name}"
            )
            engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5}
                                   if ds.type == "mysql" else {})
            try:
                with engine.connect() as conn:
                    # C6 查询超时：DB 层 statement timeout
                    if ds.type == "mysql":
                        conn.exec_driver_sql(
                            f"SET SESSION MAX_EXECUTION_TIME = {settings.query_timeout_ms}")
                    elif ds.type == "postgresql":
                        conn.execute(text(
                            f"SET LOCAL statement_timeout = '{settings.query_timeout_ms}ms'"))
                    result = conn.execute(text(final_sql))
                    columns = list(result.keys())
                    rows = [list(r) for r in result.fetchmany(settings.query_max_rows + 1)]
            finally:
                engine.dispose()
        finally:
            db.close()
    finally:
        limiter.release()

    truncated = len(rows) > settings.query_max_rows
    rows = rows[:settings.query_max_rows]
    latency = int((time.time() - start) * 1000)
    out = {
        "sql": final_sql,
        "original_sql": sql,
        "permission": injected or "1=1",
        "row_rule_summary": row_rule_summary,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "latency_ms": latency,
        "dialect": dialect,
    }
    if cost_estimate:
        out["cost_estimate"] = cost_estimate
        out["cost_warning"] = cost_estimate["level"] == "warn"
    return out


def to_jsonable(rows: list[list[Any]]) -> list[list[Any]]:
    out = []
    for row in rows:
        out.append([
            item.isoformat() if hasattr(item, "isoformat") else
            (float(item) if isinstance(item, int) and abs(item) > 2 ** 53 else item)
            for item in row
        ])
    return out
