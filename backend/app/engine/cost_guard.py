"""C2 · Explain 成本门槛：SQL 执行前成本预估与分档决策（allow / warn / deny）。

- MySQL：EXPLAIN 取根节点估计 rows，软/硬门槛 1e6 / 1e7
- PostgreSQL：EXPLAIN 取计划总 cost，软/硬门槛 5e4 / 5e5
- 其他方言或 EXPLAIN 不可用：结构启发式评分（0-100），软/硬 50 / 80
- 任一环节失败均安全放行（deny 只来自明确测量），并在 detail 中标记降级
"""
import logging
import re
import time
from dataclasses import dataclass

from sqlalchemy import create_engine, text

from ..config import get_settings
from ..security import aes_decrypt

logger = logging.getLogger(__name__)

_EXPLAIN_PG_COST = re.compile(r"cost=(\d+(?:\.\d+)?)\.\.(\d+(?:\.\d+)?)")


@dataclass
class CostEstimate:
    level: str          # allow / warn / deny
    engine: str         # explain_mysql / explain_pg / heuristic / unavailable
    metric: str         # rows / cost / score
    value: float
    soft: float
    hard: float
    detail: str = ""    # 计划片段（截断）


def _to_float(v) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _build_url(ds) -> str:
    from urllib.parse import quote_plus

    scheme = "postgresql+psycopg2" if ds.type == "postgresql" else "mysql+pymysql"
    return (f"{scheme}://{quote_plus(ds.user)}:{quote_plus(aes_decrypt(ds.password_enc))}"
            f"@{ds.host}:{ds.port}/{ds.db_name}")


def _explain_mysql(engine, sql: str, timeout_ms: int) -> tuple[float | None, str]:
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME = {timeout_ms}")
            result = conn.execute(text(f"EXPLAIN {sql}"))
            col_names = list(result.keys())
            rows = result.fetchall()
            conn.exec_driver_sql("SET SESSION MAX_EXECUTION_TIME = 0")
        # 传统表格格式：列名含 rows
        if "rows" in col_names:
            idx = col_names.index("rows")
            vals = [_to_float(r[idx]) for r in rows if _to_float(r[idx]) is not None]
            if vals:
                detail = " | ".join(str(r[:10]) for r in rows[:5])
                return max(vals), detail[:300]
        # 树形格式（MySQL 8.0.20+ 默认）：单列 EXPLAIN 文本，提取 rows=N
        plan_text = "\n".join(str(r[0]) for r in rows if r and r[0] is not None)
        import re

        m_rows = [float(x) for x in re.findall(r"rows=(\d+(?:\.\d+)?)", plan_text)]
        if m_rows:
            return max(m_rows), plan_text[:300]
        return None, plan_text[:200]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cost_guard] mysql explain 失败: %s", exc)
        return None, f"explain 失败: {exc}"[:200]


def _explain_pg(engine, sql: str, timeout_ms: int) -> tuple[float | None, str]:
    try:
        with engine.connect() as conn:
            conn.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
            result = conn.execute(text(f"EXPLAIN {sql}"))
            plan = "\n".join(r[0] for r in result.fetchall())
        costs = [_to_float(m[2]) for m in _EXPLAIN_PG_COST.finditer(plan)]
        if not costs:
            return None, plan[:200]
        return max(costs), plan[:300]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cost_guard] pg explain 失败: %s", exc)
        return None, f"explain 失败: {exc}"[:200]


def _heuristic(sql: str, dialect: str) -> tuple[float, str]:
    """结构启发式评分（0-100）：无 EXPLAIN 能力时兜底。"""
    import sqlglot
    from sqlglot import exp as gexp

    score = 0.0
    hints: list[str] = []
    try:
        ast = sqlglot.parse_one(sql, read=dialect)
        tables = {str(t.name) for t in ast.find_all(gexp.Table)}
        joins = list(ast.find_all(gexp.Join))
        group_bys = list(ast.find_all(gexp.Group))
        has_where = any(s.args.get("where") is not None
                        for s in ast.find_all(gexp.Select))
        n_tables = len(tables)
        if n_tables > 5:
            score += 40
            hints.append(f"表数{n_tables}>5")
        elif n_tables > 3:
            score += 20
            hints.append(f"表数{n_tables}")
        if len(joins) >= 3:
            score += 20
            hints.append(f"JOIN数{len(joins)}")
        if group_bys:
            score += 10
            hints.append("GROUP BY")
        if not has_where:
            score += 20
            hints.append("无WHERE")
        if n_tables <= 2 and len(joins) <= 1 and has_where:
            score = min(score, 20)
    except Exception as exc:  # noqa: BLE001
        return 50.0, f"启发式解析失败: {exc}"[:100]
    return min(score, 100.0), "；".join(hints) or "简单查询"


def estimate_cost(sql: str, dialect: str, datasource) -> CostEstimate:
    """对 SQL 做成本预估与分档。任何失败安全放行（deny 只来自明确测量）。"""
    settings = get_settings()
    timeout_ms = settings.cost_guard_explain_timeout_ms
    soft = hard = 0.0
    metric = "score"
    engine_kind = "heuristic"
    value: float | None = None
    detail = ""

    url = _build_url(datasource)
    engine = None
    try:
        if datasource.type == "mysql":
            engine = create_engine(url, pool_pre_ping=True,
                                   connect_args={"connect_timeout": 5})
            value, detail = _explain_mysql(engine, sql, timeout_ms)
            if value is not None:
                engine_kind, metric = "explain_mysql", "rows"
                soft, hard = settings.cost_guard_mysql_rows_soft, settings.cost_guard_mysql_rows_hard
        elif datasource.type == "postgresql":
            engine = create_engine(url, pool_pre_ping=True)
            value, detail = _explain_pg(engine, sql, timeout_ms)
            if value is not None:
                engine_kind, metric = "explain_pg", "cost"
                soft, hard = settings.cost_guard_pg_cost_soft, settings.cost_guard_pg_cost_hard
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cost_guard] 连接失败，降级启发式: %s", exc)
        detail = f"连接失败: {exc}"[:200]
    finally:
        if engine is not None:
            engine.dispose()

    if value is None:
        value, hdetail = _heuristic(sql, dialect)
        soft, hard = 50.0, 80.0
        engine_kind = "heuristic"
        detail = (detail + " | " + hdetail).strip(" |")[:300]

    level = "deny" if value >= hard else ("warn" if value >= soft else "allow")
    return CostEstimate(level=level, engine=engine_kind, metric=metric,
                        value=value, soft=soft, hard=hard, detail=detail)


def guard_sql(sql: str, dialect: str, datasource) -> CostEstimate | None:
    """执行前守卫：成本门槛开启时返回 CostEstimate；关闭时返回 None。"""
    if not get_settings().cost_guard_enabled:
        return None
    return estimate_cost(sql, dialect, datasource)


def format_estimate(est: CostEstimate) -> dict:
    return {"level": est.level, "engine": est.engine, "metric": est.metric,
            "value": est.value, "soft": est.soft, "hard": est.hard,
            "detail": est.detail,
            "message": (f"{est.metric}={est.value:,.0f}（软阈值 {est.soft:,.0f}，"
                        f"硬阈值 {est.hard:,.0f}）" if est.metric in ("rows", "cost")
                        else f"风险分={est.value:.0f}/100")}
