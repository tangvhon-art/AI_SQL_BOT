"""从最终 SQL 提取口径溯源信息。

LLM 兜底路径（无字段级映射 mapping=None）时，SQL 是唯一权威口径来源：
- 数据源表：FROM/JOIN 中的基础表（解析 CTE 后取其真实来源表）
- 指标/维度/条件：尽力而为地从外层 SELECT 提取聚合表达式、分组列与 WHERE 条件列

解析失败一律返回 None，由上层回退到 LLM 选中表/空展示，绝不阻塞主流程。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def extract_sql_tables(sql: str, dialect: str = "mysql") -> list[str]:
    """提取 SQL 涉及的基础数据表（去重、排除 CTE 别名）。"""
    try:
        import sqlglot
        ast = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:  # noqa: BLE001
        logger.info("[sql_trace] 解析失败: %s", exc)
        return []
    try:
        cte_names = {c.alias_or_name for c in ast.find_all(sqlglot.exp.CTE) if c.alias_or_name}
        tables: list[str] = []
        for t in ast.find_all(sqlglot.exp.Table):
            n = t.name
            if n and n not in cte_names and n not in tables:
                tables.append(n)
        return tables
    except Exception as exc:  # noqa: BLE001
        logger.info("[sql_trace] 提取表失败: %s", exc)
        return []


def extract_sql_trace(sql: str, dialect: str = "mysql") -> dict | None:
    """尽力而为提取 {tables, metrics, dimensions, filters}；失败返回 None。"""
    try:
        import sqlglot
        from sqlglot import exp
        ast = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:  # noqa: BLE001
        logger.info("[sql_trace] 解析失败: %s", exc)
        return None
    try:
        cte_names = {c.alias_or_name for c in ast.find_all(exp.CTE) if c.alias_or_name}
        tables: list[str] = []
        for t in ast.find_all(exp.Table):
            n = t.name
            if n and n not in cte_names and n not in tables:
                tables.append(n)

        def _source_table(sel) -> str:
            """SELECT 的直接来源：FROM 首表；若为 CTE 引用则递归取其真实来源表。"""
            from_ = sel.args.get("from")
            if from_:
                tbl = from_.this
                if isinstance(tbl, exp.Table) and tbl.name in cte_names:
                    for cte in ast.find_all(exp.CTE):
                        if cte.alias_or_name == tbl.name:
                            inner = cte.this
                            if isinstance(inner, exp.Select):
                                return _source_table(inner)
                if isinstance(tbl, exp.Table):
                    return tbl.name
            return ""

        metrics: list[dict] = []
        dims: list[dict] = []
        filters: list[dict] = []
        for sel in ast.find_all(exp.Select):
            src = _source_table(sel)
            for proj in sel.expressions:
                alias = proj.alias if isinstance(proj, exp.Alias) else None
                for agg in proj.find_all(exp.AggFunc):
                    name = alias or agg.sql()
                    if name and not any(m["name"] == name for m in metrics):
                        metrics.append({
                            "name": name,
                            "table": src or "",
                            "column": agg.sql() or "",
                            "agg": str(agg.key).upper() or "sum",
                        })
            for g in sel.args.get("group", []) or []:
                txt = g.sql()
                if txt and not any(d["name"] == txt for d in dims):
                    dims.append({"name": txt, "table": src or "", "column": txt})
            where = sel.args.get("where")
            if where:
                for col in where.find_all(exp.Column):
                    cname = col.name
                    if cname and cname not in [f["column"] for f in filters]:
                        filters.append({"field": cname, "op": "", "value": "",
                                        "column": cname, "table": src or ""})
        return {"tables": tables, "metrics": metrics,
                "dimensions": dims, "filters": filters}
    except Exception as exc:  # noqa: BLE001
        logger.info("[sql_trace] 提取失败: %s", exc)
        return None
