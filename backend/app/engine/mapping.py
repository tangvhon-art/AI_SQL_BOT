"""L3 数据源三层映射：指标→字段、维度→分组、条件→过滤 + 权限前置拦截。

映射优先顺序：
1. 意图词典（intent_dict，管理员配置，term/aliases 精确匹配，target 明确）
2. Schema 字段注释/字段名语义匹配（表/字段 comment 必采，人工补录）
3. 匹配不到 → 返回空，由上层触发澄清或 LLM 兜底
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from .text_utils import contains_term, match_score
from .schema_types import is_numeric, is_date, is_string, is_time_field, is_system_field, is_id_field
from .biz_lexicon import match as lex_match, get as lex_get
from ..database import SessionLocal
from ..models import ColumnMeta, TableMeta


class MappingError(Exception):
    """映射失败（含权限不足），message 面向用户友好提示。"""


class PermissionMappingError(MappingError):
    pass


def _text_of(col: ColumnMeta) -> str:
    """字段的可匹配文本：注释 + 字段名。"""
    parts = [p for p in (col.comment or "", col.column_name) if p]
    return " ".join(parts).lower()


def fetch_schemas(datasource_id: int) -> list[str]:
    """返回数据源已采集的 schema（库/项目）清单，按名称排序。"""
    db = SessionLocal()
    try:
        rows = (db.query(TableMeta.schema_name)
                .filter(TableMeta.datasource_id == datasource_id,
                        TableMeta.deprecated.is_(False))
                .distinct().all())
        out = []
        for (sn,) in rows:
            if sn and sn.strip():
                out.append(sn)
        return sorted(out)
    finally:
        db.close()


def fetch_schema_candidates(datasource_id: int,
                            schema_name: str | None = None,
                            tables: list[str] | None = None) -> dict[str, list[dict]]:
    """从 Schema 提取候选要素（供规则引擎实体抽取与澄清候选展示）：
    {metrics: [{table, column, comment, data_type, agg}],
     dimensions: [{table, column, comment, data_type}],
     time_fields: [{table, column, comment}]}
    schema_name 非空时按 schema（项目/库）限定候选表范围；
    tables 非空时只返回这些表（用户确认表后的指标/维度解析必须限定在确认表内）。
    """
    db = SessionLocal()
    try:
        q = (db.query(TableMeta)
             .filter(TableMeta.datasource_id == datasource_id,
                     TableMeta.deprecated.is_(False)))
        if schema_name:
            q = q.filter(TableMeta.schema_name == schema_name)
        if tables:
            q = q.filter(TableMeta.table_name.in_(tables))
        tables = q.all()
        metrics, dims, times = [], [], []
        for t in tables:
            cols = (db.query(ColumnMeta)
                    .filter(ColumnMeta.table_meta_id == t.id)
                    .order_by(ColumnMeta.ordinal).all())
            for c in cols:
                cname = c.column_name.lower()
                if is_system_field(cname):
                    continue
                # ID/外键类字段（xx_id 或 id 结尾）不参与指标/维度匹配：
                # 防止规则引擎把 project_id 之类外键当 SUM 指标，或把 ID 当分组维度
                if is_id_field(cname):
                    continue
                item = {"table": t.table_name, "table_comment": t.comment or "",
                        "column": c.column_name, "comment": c.comment or "",
                        "data_type": c.data_type or ""}
                if is_time_field(c.column_name, c.comment, c.data_type):
                    times.append(item)
                elif is_numeric(c.data_type):
                    metrics.append({**item, "agg": "sum"})
                elif is_string(c.data_type) or not is_numeric(c.data_type):
                    dims.append(item)
        return {"metrics": metrics, "dimensions": dims, "time_fields": times}
    finally:
        db.close()


def _first_candidate_table(candidates: dict) -> str:
    for bucket in ("metrics", "dimensions", "time_fields"):
        for it in candidates.get(bucket, []):
            t = it.get("table") or ""
            if t:
                return t
    return ""


def _match_dict_entry(dicts: list[dict], term: str) -> dict | None:
    for d in dicts:
        if term and (term == d["term"] or any(term == a for a in d["aliases"])):
            return d
        if term and d["term"] and len(d["term"]) >= 2 and d["term"] in term:
            return d
    return None


def _lookup_metric(db: Session, datasource_id: int, term: str, candidates: dict) -> dict | None:
    """指标→字段：词典优先，其次数值字段注释匹配。"""
    dicts = db  # 词典由上层通过 spec 传递；此处仅 Schema 匹配
    items = [m for m in candidates["metrics"] if contains_term(m["comment"], term)
             or contains_term(m["column"], term)]
    best = None
    best_s = -1
    for it in items:
        text = (it["comment"] + " " + it["column"]).lower()
        s = match_score(text, term)
        if s > best_s:
            best_s, best = s, it
    if best_s <= 0:
        best = None
    return best


def _lookup_dimension(db: Session, datasource_id: int, term: str, candidates: dict) -> dict | None:
    items = [d for d in candidates["dimensions"] if contains_term(d["comment"], term)
             or contains_term(d["column"], term)]
    best = None
    best_s = -1
    for it in items:
        text = (it["comment"] + " " + it["column"]).lower()
        s = match_score(text, term)
        if s > best_s:
            best_s, best = s, it
    if best_s <= 0:
        best = None
    return best


def _lookup_time_field(candidates: dict) -> dict | None:
    """时间字段：优先注释含 时间/日期 的字段。"""
    times = candidates["time_fields"]
    if not times:
        return None
    for t in times:
        c = (t["comment"] + " " + t["column"]).lower()
        if "创建时间" in c or "create_time" in c or "下单时间" in c or "create" in c:
            return t
    return times[0]


def _strip_bucket_suffix(term: str) -> str:
    """分箱维度名去后缀：'文件大小范围'→'文件大小'（范围/区间/分布 是分箱语义词）。"""
    for suf in lex_get("bucket"):
        if term.endswith(suf) and len(term) > len(suf):
            return term[:-len(suf)]
    return term


def map_spec_to_schema(spec: Any, datasource_id: int, user_id: int,
                       dicts: dict[str, list[dict]] | None = None,
                       tables: list[str] | None = None) -> dict:
    """把 QuerySpec 映射到数据源真实字段。

    返回 {table, metrics:[{name, column, comment, agg, unit}],
          dimensions:[{name, column, comment}],
          filters:[{field, column, op, value}],
          time_field:{column, comment} | None,
          denied:[...]}
    任一关键映射失败抛 MappingError（友好提示）。
    tables: 用户已确认的查询表（表澄清确认轮传入），映射候选限定在这些表内。
    """
    # schema（项目/库）限定候选范围：优先 spec.schema_name，其次问题提取
    schema_name = getattr(spec, "schema", "") or None
    candidates = fetch_schema_candidates(datasource_id, schema_name, tables)
    db = SessionLocal()
    try:
        dicts = dicts or {}
        metric_dicts = dicts.get("metric", [])
        dim_dicts = dicts.get("dimension", [])
        filter_dicts = dicts.get("filter", [])

        # ---- 指标映射 ----
        mapped_metrics = []
        for m in spec.metrics:
            # 点名表名查询（如 "agent_tasks 总共有多少条" → alias=__table__:agent_tasks）：
            # 直接确认表结构（schema 限定内存在该表）→ COUNT(*) 该表
            _marker = (m.alias or m.name or "")
            if _marker.startswith("__table__:"):
                tbl = _marker.split(":", 1)[1]
                tm = (db.query(TableMeta)
                      .filter(TableMeta.datasource_id == datasource_id,
                              TableMeta.table_name == tbl,
                              TableMeta.deprecated.is_(False)).first())
                if not tm:
                    raise MappingError(f"未找到数据表「{tbl}」")
                if schema_name and tm.schema_name != schema_name:
                    raise MappingError(f"数据表「{tbl}」不在查询范围 {schema_name} 内")
                mapped_metrics.append({
                    "name": m.name, "column": "*", "table": tbl,
                    "comment": tm.comment or tbl, "agg": m.agg or "count",
                    "unit": "",
                })
                continue
            term = m.name or ""
            ent = _match_dict_entry(metric_dicts, term)
            col = None
            if ent and ent["target_table"] and ent["target_column"]:
                col = {"table": ent["target_table"], "column": ent["target_column"],
                       "comment": ent["term"], "data_type": "", "agg": ent["agg"] or m.agg}
            if col is None:
                col = _lookup_metric(db, datasource_id, term, candidates)
            if col is None:
                # COUNT(*) 类指标不依赖具体字段（如"任务数量"）：表在维度映射后回填
                if m.agg == "count":
                    col = {"table": "__PENDING__", "column": "*",
                           "comment": term, "data_type": "", "agg": "count"}
                else:
                    raise MappingError(f"未找到指标「{term}」对应的数据字段。可查指标："
                                       + "、".join(x["comment"] or x["column"]
                                                   for x in candidates["metrics"][:8]) or "（无）")
            mapped_metrics.append({
                "name": term, "column": col["column"], "table": col["table"],
                "comment": col["comment"] or col["column"],
                "agg": (ent or {}).get("agg") or m.agg or "sum",
                "unit": (ent or {}).get("unit") or m.unit or "",
            })

        # ---- 维度映射 ----
        mapped_dims = []
        for d in spec.dimensions:
            term = d.name or ""
            bucket = getattr(d, "bucket", None)
            ent = _match_dict_entry(dim_dicts, term)
            col = None
            if ent and ent["target_table"] and ent["target_column"]:
                col = {"table": ent["target_table"], "column": ent["target_column"],
                       "comment": ent["term"]}
            if col is None:
                col = _lookup_dimension(db, datasource_id, term, candidates)
            if col is None:
                col = _lookup_dimension(db, datasource_id, d.alias or term, candidates)
            # 分箱维度（范围分布）：数值字段（大小/金额/时长）在候选里属 metrics，
            # 需从指标候选中回找，否则「文件大小范围」这类维度映射不到 size 字段
            if col is None and bucket is not None:
                for mt in (term, _strip_bucket_suffix(term), d.alias or ""):
                    if not mt:
                        continue
                    col = _lookup_metric(db, datasource_id, mt, candidates)
                    if col:
                        break
            if col is None:
                raise MappingError(f"未找到维度「{term}」对应的分组字段。可查维度："
                                   + "、".join(x["comment"] or x["column"]
                                               for x in candidates["dimensions"][:8]) or "（无）")
            item = {"name": term, "column": col["column"],
                    "table": col["table"], "comment": col["comment"] or col["column"]}
            # 分箱维度（范围分布）：把 bucket 定义透传给 SQL 层（生成 CASE WHEN 直方图）
            if bucket is not None and (bucket.ranges or bucket.labels):
                b_field = (bucket.field or "").strip() or col["column"]
                # bucket.field 显式指定且与映射列不同时，确认该字段在候选范围内存在
                if (bucket.field or "").strip() and b_field != col["column"]:
                    exists = any(c["column"] == b_field for c in
                                 candidates["dimensions"] + candidates["metrics"])
                    if exists:
                        col = {"table": col["table"], "column": b_field,
                               "comment": col["comment"]}
                        item["column"] = b_field
                item["bucket"] = {
                    "field": b_field,
                    "ranges": [[float(x), float(y)] for x, y in (bucket.ranges or [])],
                    "labels": list(bucket.labels or []),
                    "unit": bucket.unit or "",
                    "left_closed": bool(bucket.left_closed),
                }
            mapped_dims.append(item)

        # 回填 COUNT(*) 类指标的归属表：维度表优先，其次 schema 候选首表
        for mm in mapped_metrics:
            if mm.get("table") == "__PENDING__":
                mm["table"] = next((d.get("table", "") for d in mapped_dims if d.get("table")),
                                   "") or _first_candidate_table(candidates)

        # ---- 条件映射 ----
        mapped_filters = []
        for f in spec.filters:
            term = f.field or ""
            ent = _match_dict_entry(filter_dicts, term)
            col = None
            if ent and ent["target_table"] and ent["target_column"]:
                col = {"table": ent["target_table"], "column": ent["target_column"],
                       "comment": ent["term"]}
            if col is None:
                # 条件字段匹配：优先字符串维度，其次任意字段
                for cand in candidates["dimensions"] + candidates["metrics"]:
                    if contains_term(cand["comment"], term) or contains_term(cand["column"], term):
                        col = cand
                        break
            if col is None:
                continue  # 条件映射不到时跳过（不阻塞），由上层提示
            mapped_filters.append({
                "field": term, "column": col["column"], "table": col["table"],
                "comment": col["comment"] or col["column"],
                "op": f.op or "=", "value": f.value,
            })

        time_field = _lookup_time_field(candidates)
        # 时间字段必须与主表（第一个指标的归属表）一致，否则 SQL 会引用不存在的列
        main_table = ""
        for m in mapped_metrics:
            if m.get("table"):
                main_table = m["table"]
                break
        if main_table and candidates["time_fields"]:
            same_table = [t for t in candidates["time_fields"] if t["table"] == main_table]
            if same_table:
                time_field = same_table[0]
            else:
                time_field = None  # 主表无时间字段 → 趋势/时间过滤走 LLM 兜底
        elif not main_table and not mapped_dims:
            # 无指标无维度（如明细清单类）→ 不伪造时间字段拼单表 SQL，交由 LLM 字段检索
            time_field = None

        # ---- 权限前置拦截（映射完成后、SQL 生成前） ----
        denied = _check_permission(datasource_id, user_id, mapped_metrics,
                                   mapped_dims, mapped_filters)
        if denied:
            raise PermissionMappingError(
                "您暂无以下字段的查询权限：" + "、".join(denied[:5])
                + "；请更换指标或联系管理员授权")

        return {"table": None, "metrics": mapped_metrics, "dimensions": mapped_dims,
                "filters": mapped_filters, "time_field": time_field, "denied": []}
    finally:
        db.close()


def _check_permission(datasource_id: int, user_id: int,
                      metrics: list[dict], dims: list[dict],
                      filters: list[dict]) -> list[str]:
    """字段级权限拦截：引用字段命中 deny → 返回字段列表（黑名单优先，与 executor 口径一致）。"""
    from ..engine.permission import compute_permissions
    from ..database import SessionLocal
    from ..models import Datasource, TableMeta

    db = SessionLocal()
    try:
        ds = db.query(Datasource).get(datasource_id)
        if not ds:
            return []
        allow, deny = compute_permissions(user_id, ds.workspace_id)
        if not deny:
            return []
        # 收集被引用字段
        refs = [(m["table"], m["column"]) for m in metrics]
        refs += [(d["table"], d["column"]) for d in dims]
        refs += [(f["table"], f["column"]) for f in filters]
        denied = []
        for tbl, col in refs:
            tm = (db.query(TableMeta)
                  .filter(TableMeta.datasource_id == datasource_id,
                          TableMeta.table_name == tbl).first())
            if tm:
                key = f"{datasource_id}.{tm.schema_name}.{tm.table_name}.{col}"
                if key in deny:
                    denied.append(f"{tm.table_name}.{col}")
        return denied
    finally:
        db.close()
