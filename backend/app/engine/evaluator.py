"""C4 · 评测闭环：用例 → 批量评测 → 指标汇总。

评测执行与 chat 主链路一致（generate_sql_stream），mock 模式不真实连库：
- sql_generated / intent：来自生成器 result 事件
- sql_executable：sqlglot 语法 + 权限改写不抛异常
- sql_correct：期望 SQL 存在时语义等价判定（表集合 + 列集合 + 聚合函数集合）
- e2e_ok：真实模式=执行成功且有数据；mock 模式=生成 + 可执行 + 表命中
批次在后台线程执行，EvalRun.status 轮询推进。
"""
import logging
import threading
import time

import sqlglot
from sqlglot import exp

from ..database import SessionLocal
from ..executor import validate_sql
from ..models import EvalCase, EvalResult, EvalRun
from ..config import get_settings

logger = logging.getLogger(__name__)


def _sql_shape(sql: str, dialect: str) -> dict:
    """SQL 结构特征：表集合 / 投影列（去别名） / WHERE 引用列 / 聚合函数。解析失败返回 None。"""
    try:
        ast = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001
        return None
    tables = {str(t.name).lower() for t in ast.find_all(exp.Table)}
    proj_cols: set[str] = set()
    agg_funcs: set[str] = set()
    for select in ast.find_all(exp.Select):
        for p in (select.args.get("expressions") or []):
            for c in p.find_all(exp.Column):
                proj_cols.add(str(c.name).lower())
            for a in p.find_all(exp.AggFunc):
                agg_funcs.add(a.sql_name().lower())
    where_cols: set[str] = set()
    for select in ast.find_all(exp.Select):
        w = select.args.get("where")
        if w is not None:
            for c in w.find_all(exp.Column):
                where_cols.add(str(c.name).lower())
    return {"tables": tables, "proj_cols": proj_cols,
            "where_cols": where_cols, "aggs": agg_funcs}


def sql_semantic_eq(actual: str, expected: str, dialect: str) -> bool:
    """语义等价判定：表集合一致 + 投影列重叠 ≥ 0.8 + 聚合函数一致。"""
    a, e = _sql_shape(actual, dialect), _sql_shape(expected, dialect)
    if a is None or e is None:
        return False
    if a["tables"] != e["tables"]:
        return False
    if a["aggs"] != e["aggs"]:
        return False
    if not e["proj_cols"]:
        return True
    overlap = len(a["proj_cols"] & e["proj_cols"]) / len(e["proj_cols"])
    return overlap >= 0.8


def _extract_sql_tables(sql: str, dialect: str) -> set[str]:
    shape = _sql_shape(sql, dialect)
    return set(shape["tables"]) if shape else set()


def _eval_one(case: EvalCase, ws_id: int, llm, mock_execute: bool, db) -> dict:
    """评测单用例，返回判定明细。"""
    from ..engine.nl2sql import generate_sql_stream
    from ..engine.permission import rewrite_sql_for_user

    start = time.time()
    detail: dict = {}
    result = None
    sql = ""
    llm_used = False
    tokens = {}
    try:
        for event in generate_sql_stream(case.datasource_id, ws_id, case.question,
                                         history=[], llm=llm, spec_context={}):
            if event["type"] == "result":
                result = event["result"]
            elif event["type"] == "stream":
                pass
        if result:
            sql = result.get("sql") or ""
            llm_used = bool(result.get("llm_used"))
            tokens = result.get("tokens") or {}
            detail["intent"] = result.get("intent")
            detail["explain"] = (result.get("explain") or "")[:200]
    except Exception as exc:  # noqa: BLE001
        detail["error"] = str(exc)[:300]
        logger.warning("[eval] 用例 %s 生成失败: %s", case.id, exc)

    sql_generated = bool((sql or "").strip())
    sql_executable = False
    sql_correct = None  # 三态：True/False/None(未判定)
    if sql_generated:
        try:
            dialect = "mysql"
            from ..models import Datasource
            ds = db.query(Datasource).get(case.datasource_id)
            dialect = ds.type if ds else "mysql"
            ast = validate_sql(sql, dialect)
            rewritten, injected, denied, _ = rewrite_sql_for_user(ast, dialect, case.created_by or 1)
            sql_executable = bool(rewritten) and not (denied and not injected)
        except Exception as exc:  # noqa: BLE001
            detail["exec_error"] = str(exc)[:300]
        if case.expect_sql:
            sql_correct = sql_semantic_eq(sql, case.expect_sql, dialect)

    tables_hit = False
    expect_tables = {str(t).lower() for t in (case.expect_tables_json or []) if t}
    if expect_tables:
        actual = _extract_sql_tables(sql, dialect) if sql else set()
        tables_hit = expect_tables.issubset(actual) or (not actual and False)
        detail["expect_tables"] = sorted(expect_tables)
        detail["actual_tables"] = sorted(actual)
    else:
        tables_hit = True  # 未指定期望表不判定

    e2e_ok = False
    if mock_execute:
        e2e_ok = sql_generated and sql_executable and tables_hit
    else:
        if sql_generated and sql_executable:
            try:
                from ..executor import run_query
                r = run_query(case.datasource_id, sql, case.created_by or 1)
                e2e_ok = r.get("row_count", 0) > 0
            except Exception as exc:  # noqa: BLE001
                detail["e2e_error"] = str(exc)[:300]

    latency = int((time.time() - start) * 1000)
    return {
        "intent_ok": sql_generated and (detail.get("intent") in (None, "query", "nl2sql")),
        "tables_hit": tables_hit,
        "sql_generated": sql_generated,
        "sql_executable": sql_executable,
        "sql_correct": sql_correct,
        "e2e_ok": e2e_ok,
        "latency_ms": latency,
        "llm_used": llm_used,
        "tokens": tokens,
        "error_msg": (detail.get("error") or detail.get("exec_error") or "")[:500],
        "detail": detail,
    }


def _run_batch(run_id: int, llm) -> None:
    """后台执行批次评测并落库指标。"""
    db = SessionLocal()
    try:
        run = db.query(EvalRun).get(run_id)
        if run is None:
            return
        cases = (db.query(EvalCase)
                 .filter(EvalCase.workspace_id == run.workspace_id,
                         EvalCase.status == "active")
                 .order_by(EvalCase.id.asc()).all())
        scope = run.scope_json or {}
        if scope.get("case_ids"):
            cases = [c for c in cases if c.id in scope["case_ids"]]
        elif scope.get("tags"):
            tags = set(scope["tags"])
            cases = [c for c in cases if tags & set((c.tags or "").split(","))]

        run.total = len(cases)
        db.commit()
        agg = {"intent_ok": 0, "tables_hit": 0, "sql_generated": 0,
               "sql_executable": 0, "sql_correct_true": 0, "sql_correct_judged": 0,
               "e2e_ok": 0, "latency_sum": 0}
        for case in cases:
            d = _eval_one(case, run.workspace_id, llm,
                          bool(run.mock_execute), db)
            res = EvalResult(run_id=run.id, case_id=case.id,
                             intent_ok=d["intent_ok"], tables_hit=d["tables_hit"],
                             sql_generated=d["sql_generated"],
                             sql_executable=d["sql_executable"],
                             sql_correct=d["sql_correct"],  # 三态：True/False/None(未判定)
                             e2e_ok=d["e2e_ok"], latency_ms=d["latency_ms"],
                             llm_used=d["llm_used"], tokens_json=d["tokens"],
                             error_msg=d["error_msg"], detail_json=d["detail"])
            db.add(res)
            for k in ("intent_ok", "tables_hit", "sql_generated", "sql_executable", "e2e_ok"):
                if d[k]:
                    agg[k] += 1
            if d["sql_correct"] is not None:
                agg["sql_correct_judged"] += 1
                if d["sql_correct"]:
                    agg["sql_correct_true"] += 1
            agg["latency_sum"] += d["latency_ms"]
        db.commit()

        n = max(run.total, 1)
        metrics = {
            "intent_accuracy": round(agg["intent_ok"] / n, 4),
            "table_hit_rate": round(agg["tables_hit"] / n, 4),
            "sql_generation_rate": round(agg["sql_generated"] / n, 4),
            "sql_executable_rate": round(agg["sql_executable"] / n, 4),
            "sql_correct_rate": (round(agg["sql_correct_true"] / agg["sql_correct_judged"], 4)
                                 if agg["sql_correct_judged"] else None),
            "e2e_accuracy": round(agg["e2e_ok"] / n, 4),
            "avg_latency_ms": round(agg["latency_sum"] / n, 1),
        }
        run.status = "success"
        run.metrics_json = metrics
        run.finished_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        db.commit()
        logger.info("[eval] 批次 %s 完成: %s", run.id, metrics)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[eval] 批次 %s 失败", run.id)
        db.rollback()
        run.status = "failed"
        run.metrics_json = {"error": str(exc)[:500]}
        run.finished_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        db.commit()
    finally:
        db.close()


def start_batch(run_id: int, llm) -> None:
    """启动后台评测线程（daemon，不阻塞 API）。"""
    db = SessionLocal()
    try:
        run = db.query(EvalRun).get(run_id)
        if run:
            run.status = "running"
            run.started_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            db.commit()
    finally:
        db.close()
    t = threading.Thread(target=_run_batch, args=(run_id, llm), daemon=True)
    t.start()
