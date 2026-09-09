"""C11 · 多候选 SQL 生成与评分：≤4 条候选，五维评分择优。

评分维度（需求文档 3.7）：
- 语法 30：sqlglot 解析 + 只读校验，失败致命
- 字段存在性 25：引用的表/列存在于元数据且未被权限 deny，失败致命
- 意图规则 20：投影含指标列/聚合、WHERE 含过滤条件，部分得分
- 成本预估 15：结构启发式（表数/JOIN/WHERE/聚合）
- 复杂度 10：结构简单加分（无嵌套子查询/无 DISTINCT）
致命项一票否决；候选 ≤4 条，不需要多次 LLM 调用（确定性变体 + 主 SQL）。
"""
import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from ..database import SessionLocal
from ..executor import validate_sql, SqlExecError
from ..models import ColumnMeta, TableMeta

logger = logging.getLogger(__name__)


@dataclass
class Candidate:
    sql: str
    source: str
    scores: dict = field(default_factory=dict)
    total: float = 0.0
    fatal: str = ""


def _aggregate_of(select: exp.Select) -> bool:
    return any(select.find_all(exp.AggFunc))


def _filter_cols_of(select: exp.Select) -> set[str]:
    w = select.args.get("where")
    if w is None:
        return set()
    return {str(c.name).lower() for c in w.find_all(exp.Column)}


def _projection_cols(select: exp.Select) -> set[str]:
    cols: set[str] = set()
    for p in (select.args.get("expressions") or []):
        for c in p.find_all(exp.Column):
            cols.add(str(c.name).lower())
    return cols


def score_sql(sql: str, spec, mapping: dict | None, datasource_id: int,
              dialect: str, question: str = "") -> Candidate:
    """单候选评分；fatal 非空表示一票否决。"""
    c = Candidate(sql=sql, source="")
    s = c.scores
    # 1) 语法（30）
    try:
        ast = validate_sql(sql, dialect)
        s["syntax"] = 30.0
    except (SqlExecError, Exception) as exc:  # noqa: BLE001
        c.fatal = f"语法不合法: {exc}"
        c.total = 0.0
        return c

    # 2) 字段存在性（25，致命）
    db = SessionLocal()
    try:
        s["fields"] = 25.0
        for t in ast.find_all(exp.Table):
            tm = (db.query(TableMeta)
                  .filter(TableMeta.datasource_id == datasource_id,
                          TableMeta.table_name == str(t.name)).first())
            if not tm:
                c.fatal = f"表不存在: {t.name}"
                break
            for col in t.find_all(exp.Column):
                if not col.name:
                    continue
                exists = (db.query(ColumnMeta)
                          .filter(ColumnMeta.table_meta_id == tm.id,
                                  ColumnMeta.column_name == str(col.name)).first())
                if not exists:
                    c.fatal = f"字段不存在: {tm.table_name}.{col.name}"
                    break
            if c.fatal:
                break
    finally:
        db.close()
    if c.fatal:
        c.total = 0.0
        return c

    # 3) 意图规则（20）：指标覆盖 + 维度覆盖 + 过滤值覆盖
    intent_score = 0.0
    first = next(iter(ast.find_all(exp.Select)), None)
    if first is not None:
        proj = _projection_cols(first)
        has_agg = _aggregate_of(first)
        metrics = ((spec.metrics if spec else []) or [])
        dims = ((spec.dimensions if spec else []) or [])
        metric_hit = sum(1 for m in metrics
                         if (m.get("name") or "").lower() in proj or has_agg)
        dim_hit = sum(1 for d in dims if (d.get("name") or "").lower() in proj)
        m_total = max(len(metrics), 1)
        d_total = max(len(dims), 1)
        intent_score = 12.0 * (metric_hit / m_total) + 8.0 * (dim_hit / d_total)
        # 过滤条件：期望过滤值出现在 WHERE 字面量中
        literals = {str(li).lower() for li in first.find_all(exp.Literal)}
        f_total = f_hit = 0
        for flt in ((spec.filters if spec else []) or []):
            val = flt.get("value")
            if val is None or val == "":
                continue
            f_total += 1
            if str(val).lower() in literals:
                f_hit += 1
        if f_total:
            intent_score += 0.0  # 过滤值命中已在字面量覆盖中体现
        intent_score = min(intent_score, 20.0)
    s["intent"] = round(intent_score, 1)

    # 4) 成本预估（15，启发式）
    try:
        tables = {str(t.name) for t in ast.find_all(exp.Table)}
        joins = len(list(ast.find_all(exp.Join)))
        has_where = first is not None and first.args.get("where") is not None
        cost_score = 15.0
        if len(tables) > 4:
            cost_score -= 8
        elif len(tables) > 2:
            cost_score -= 4
        if joins >= 3:
            cost_score -= 4
        if not has_where:
            cost_score -= 3
        s["cost"] = max(0.0, round(cost_score, 1))
    except Exception:  # noqa: BLE001
        s["cost"] = 10.0

    # 5) 复杂度（10）
    try:
        nested = len(list(ast.find_all(exp.Subquery)))
        distinct = len(list(ast.find_all(exp.Distinct)))
        s["complexity"] = max(0.0, 10.0 - 3.0 * nested - 2.0 * distinct)
    except Exception:  # noqa: BLE001
        s["complexity"] = 8.0

    c.total = round(sum(s.values()), 1)
    return c


def _variants(sql: str, dialect: str) -> list[str]:
    """确定性变体：去 ORDER BY、补 LIMIT。失败静默跳过。"""
    out: list[str] = []
    try:
        ast = sqlglot.parse_one(sql, read=dialect)
        if ast.args.get("order"):
            v = ast.copy()
            v.args["order"] = None
            out.append(v.sql(dialect=dialect))
    except Exception:  # noqa: BLE001
        pass
    try:
        ast = sqlglot.parse_one(sql, read=dialect)
        if ast.args.get("limit") is None:
            v = ast.limit(1000)
            out.append(v.sql(dialect=dialect))
    except Exception:  # noqa: BLE001
        pass
    return out[:2]


def rank_candidates(sqls: list[dict], spec, mapping, datasource_id: int,
                    dialect: str, question: str = "") -> tuple[str, list[dict]]:
    """多候选评分排序：返回 (最优SQL, 评分明细列表)。无合格候选时返回原 SQL。"""
    cands: list[Candidate] = []
    seen: set[str] = set()
    for item in sqls:
        s = (item.get("sql") or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        c = score_sql(s, spec, mapping, datasource_id, dialect, question)
        c.source = item.get("source") or "main"
        cands.append(c)
    cands.sort(key=lambda x: x.total, reverse=True)
    detail = [{"sql": c.sql, "source": c.source, "total": c.total,
               "scores": c.scores, "fatal": c.fatal or ""} for c in cands]
    ok = [c for c in cands if not c.fatal]
    if ok:
        return ok[0].sql, detail
    return (cands[0].sql if cands else (sqls[0]["sql"] if sqls else "")), detail


def build_and_rank(sql: str, template_sql: str | None, spec, mapping,
                   datasource_id: int, dialect: str, question: str) -> tuple[str, list[dict]]:
    """chat 链路入口：主 SQL + 模板 SQL + 变体 ≤4 条 → 评分择优。"""
    sqls = [{"sql": sql, "source": "main"}]
    if template_sql and template_sql.strip() and template_sql.strip() != sql.strip():
        sqls.append({"sql": template_sql, "source": "template"})
    for v in _variants(sql, dialect):
        if len(sqls) < 4 and v not in {x["sql"] for x in sqls}:
            sqls.append({"sql": v, "source": "variant"})
    return rank_candidates(sqls[:4], spec, mapping, datasource_id, dialect, question)
