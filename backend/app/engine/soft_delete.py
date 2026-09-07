"""SQL 自动注入逻辑删除条件（is_deleted = 0）。

规则：
1. 用户问题中明确提及"删除/软删/已删/回收站/全部数据"等关键词时不注入
2. SQL 中已包含 is_deleted 条件时不注入
3. 仅对实际存在 is_deleted 字段的表注入
4. 注入格式：别名.is_deleted = 0（多表用 AND 连接）
"""
import logging

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

# 用户明确要求查软删数据时跳过注入的关键词
_SOFT_DELETE_QUERY_KEYWORDS = ("删除", "软删", "已删", "回收站", "已删除", "包含删除", "全部数据", "所有数据")


def inject_soft_delete(sql: str, datasource_id: int, question: str, db) -> str:
    """为 SQL 中存在 is_deleted 字段的表自动注入 is_deleted = 0 条件。"""
    if not sql or not sql.strip():
        return sql

    # 1. 用户明确要求查软删数据时跳过
    if any(kw in question for kw in _SOFT_DELETE_QUERY_KEYWORDS):
        logger.info("用户问题包含软删查询关键词，跳过 is_deleted 注入")
        return sql

    # 2. SQL 已有 is_deleted 条件时跳过
    if "is_deleted" in sql.lower():
        return sql

    # 3. 解析 SQL，收集表别名映射
    try:
        tree = sqlglot.parse_one(sql, read="mysql")
    except Exception as exc:  # noqa: BLE001
        logger.warning("SQL 解析失败，跳过 is_deleted 注入: %s", exc)
        return sql

    alias_to_table: dict[str, str] = {}
    for t in tree.find_all(exp.Table):
        alias_to_table[t.alias_or_name] = t.name

    if not alias_to_table:
        return sql

    # 4. 查询哪些表有 is_deleted 字段
    from ..models import ColumnMeta, TableMeta
    table_names = list(set(alias_to_table.values()))
    meta_tables = (db.query(TableMeta)
                   .filter(TableMeta.datasource_id == datasource_id,
                           TableMeta.table_name.in_(table_names))
                   .all())
    name_to_id = {t.table_name: t.id for t in meta_tables}
    if not name_to_id:
        return sql

    sd_cols = (db.query(ColumnMeta)
               .filter(ColumnMeta.table_meta_id.in_(list(name_to_id.values())),
                       ColumnMeta.column_name == "is_deleted")
               .all())
    id_to_name = {v: k for k, v in name_to_id.items()}
    sd_table_names = {id_to_name[c.table_meta_id] for c in sd_cols}

    if not sd_table_names:
        return sql

    # 5. 构造需要加条件的别名列表
    target_aliases = [a for a, tn in alias_to_table.items() if tn in sd_table_names]
    if not target_aliases:
        return sql

    # 6. 构造条件表达式；对每个 SELECT 子句（含 UNION 的左右两侧）分别注入 WHERE
    conds = []
    for a in target_aliases:
        col = exp.column("is_deleted", table=a)
        conds.append(exp.EQ(this=col, expression=exp.Literal.number(0)))

    selects = list(tree.find_all(exp.Select))
    if not selects:
        return sql
    for sel in selects:
        # 仅统计本 SELECT 自身 FROM/JOIN 作用域的表（不含 WITH CTE 内部表，避免外层误注入）
        sel_tables: set[str] = set()
        fr = sel.args.get("from_") if "from_" in sel.args else sel.args.get("from")
        if fr is not None:
            sel_tables.update(t.alias_or_name for t in fr.find_all(exp.Table))
        for j in sel.args.get("joins") or []:
            sel_tables.update(t.alias_or_name for t in j.find_all(exp.Table))
        sel_conds = [c for c in conds if c.this.table in sel_tables]
        if not sel_conds:
            continue
        existing_where = sel.args.get("where")
        if existing_where:
            combined = exp.and_(existing_where.this, *sel_conds)
            sel.set("where", exp.Where(this=combined))
        else:
            sel.set("where", exp.Where(this=exp.and_(*sel_conds)))

    new_sql = tree.sql(dialect="mysql")
    logger.info("已注入 is_deleted=0 条件，涉及表: %s", sd_table_names)
    return new_sql
