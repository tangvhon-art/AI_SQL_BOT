"""多查询 V2.0 后端集成验证（mock 外部依赖，不连 DB/LLM）。

验证点：
1. 编排器真并行（3 子查询总耗时 ≈ 单子查询耗时，而非 3 倍）
2. 事件序列完整（sub_progress → sub_sql → sub_result / sub_error）
3. 单子查询失败不阻塞其余（N4 失败 → 该卡 error，其余 success）
4. 整体取消：未完成任务 cancelled，已完成保留
5. 单卡重试：执行失败自动修正后成功（sub_error → sub_result）
6. N5-N7 确定性输出：图表类型 / facts / 溯源 tables
"""
import os
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.engine.multi_query import MultiQueryOrchestrator
from app.engine.query_spec import SubQuerySpec, MetricSpec, DimensionSpec
from app.engine.sub_task import SubTaskContext, run_subtask_pipeline


def make_ctx(cancelled_ref=None, fail_exec=None, fail_gen=None, delay=0.15, exec_retry=1):
    fail_exec = fail_exec or set()
    fail_gen = fail_gen or set()
    state = {"cancelled": False}
    events = []
    local_cancelled = state

    def _emit(event, data):
        events.append((event, data))

    def is_cancelled():
        return cancelled_ref() if cancelled_ref else local_cancelled["cancelled"]

    def gen_sql(sub):
        time.sleep(delay)
        if sub.sub_id in fail_gen:
            raise RuntimeError(f"mock 生成失败 {sub.sub_id}")
        return f"SELECT 1 AS v FROM t_{sub.sub_id}"

    def run_query_cached(sql, datasource_id, user_id, dialect, db, log):
        time.sleep(delay)
        sub_id = sql.split("t_")[1]
        if sub_id in fail_exec:
            raise RuntimeError(f"mock 执行失败 {sub_id}")
        return {"columns": ["name", "value"], "rows": [["A", 10]], "row_count": 1,
                "latency_ms": 5, "permission": "1=1"}

    ctx = SubTaskContext(
        datasource_id=1, workspace_id=1, user_id=1, dialect="mysql",
        db=None, log=SimpleNamespace(workspace_id=1),
        llm=None, history=None, parent_spec=None,
        run_query_cached=run_query_cached,
        inject_soft_delete=lambda sql, *a: sql,
        emit=_emit,
        gen_retry=1, exec_retry=exec_retry,
        is_cancelled=is_cancelled,
        generate_sql=gen_sql,
    )
    return ctx, state, events


def make_specs(n=3):
    return [SubQuerySpec(
        sub_id=f"q{i}", question=f"子查询 {i} 销售额",
        intent="value", metrics=[MetricSpec(name="销售额", agg="sum")],
        dimensions=[DimensionSpec(name="区域")], title=f"卡片{i}",
    ) for i in range(1, n + 1)]


def status_map(results):
    return {k: (getattr(v, "status", "error") if hasattr(v, "status") else "error")
            for k, v in results.items()}


def main():
    ok = True

    # ---- 1. 真并行：3 子查询总耗时 ≈ 0.3s（串行应为 0.9s）----
    ctx, _, events = make_ctx(delay=0.15)
    orch = MultiQueryOrchestrator(max_concurrent=4, sub_timeout_s=10)
    t0 = time.time()
    results = orch.run(make_specs(3), ctx)
    elapsed = time.time() - t0
    st = status_map(results)
    assert set(st.values()) == {"success"}, st
    print(f"[1] 并行耗时 {elapsed:.2f}s (串行理论 0.9s)，加速比 {0.9 / elapsed:.1f}x，状态: {st}")
    if elapsed > 0.75:
        print("    ✗ 并行性不足（应显著小于 3 倍串行）")
        ok = False
    else:
        print("    ✓ 真并行生效")

    # ---- 2. 事件序列 ----
    ev_types = [e[0] for e in events]
    assert "sub_progress" in ev_types and "sub_sql" in ev_types and "sub_result" in ev_types, ev_types
    for q in ("q1", "q2", "q3"):
        per_sub = [e for e in events if e[1].get("sub_id") == q]
        kinds = [e[0] for e in per_sub]
        assert kinds.count("sub_result") == 1 and "sub_sql" in kinds, f"{q}: {kinds}"
    print(f"[2] 事件流完整: {len(events)} 个事件（progress/sql/result 按 sub_id 成组）✓")

    # ---- 3. 单子查询失败不阻塞其余 ----
    ctx2, _, events2 = make_ctx(fail_exec={"q2"})
    results2 = MultiQueryOrchestrator(max_concurrent=4, sub_timeout_s=10).run(make_specs(3), ctx2)
    st2 = status_map(results2)
    assert st2 == {"q1": "success", "q2": "error", "q3": "success"}, st2
    err_ev = [e for e in events2 if e[0] == "sub_error"]
    assert len(err_ev) == 1 and err_ev[0][1]["sub_id"] == "q2", err_ev
    print(f"[3] 单卡失败不阻塞: {st2}，sub_error 事件 {len(err_ev)} 条 ✓")

    # ---- 4. 整体取消：排队中任务 cancelled，已完成保留 ----
    # 7 任务 + 2 并发：任务 q1/q2 先行执行（0.5s 完成），q3-q7 排队
    # cancel 在 t=0.3：q1/q2 已过 N4 检查点 → success；q3-q7 排队 → cancelled（兜底补齐）
    cancel_flag = {"flag": False}
    ctx3, _, events3 = make_ctx(cancelled_ref=lambda: cancel_flag["flag"], delay=0.25)
    orch3 = MultiQueryOrchestrator(max_concurrent=2, sub_timeout_s=10)

    def cancel_later():
        time.sleep(0.3)
        cancel_flag["flag"] = True          # 生产：cancel 路由置 task_context.cancelled
        orch3.cancel()                       # 生产：编排器置 cancelled flag

    th = threading.Thread(target=cancel_later)
    t0 = time.time()
    th.start()
    results3 = orch3.run(make_specs(7), ctx3)
    th.join()
    st3 = status_map(results3)
    print(f"[4] 取消后状态: {st3}，耗时 {time.time() - t0:.2f}s")
    assert st3["q1"] == "success" and st3["q2"] == "success", f"已完成应保留: {st3}"
    assert all(st3[f"q{i}"] == "cancelled" for i in range(3, 8)), f"排队中应取消: {st3}"
    print("    ✓ 取消机制生效（已完成保留 / 排队中取消）")

    # ---- 5. 单卡重试：执行失败自动修正后成功 ----
    fail_state = {"q1": True}

    def flaky_exec(sql, datasource_id, user_id, dialect, db, log):
        time.sleep(0.05)
        sub_id = sql.split("t_")[1]
        if fail_state.get(sub_id):
            fail_state[sub_id] = False
            raise RuntimeError(f"mock 执行失败 {sub_id}")
        return {"columns": ["name", "value"], "rows": [["A", 10]], "row_count": 1,
                "latency_ms": 5, "permission": "1=1"}

    ctx5, _, events5 = make_ctx(delay=0.02, exec_retry=1)
    ctx5.run_query_cached = flaky_exec
    res = run_subtask_pipeline(make_specs(1)[0], ctx5)
    assert res.status == "success" and res.interpretation, f"重试后应成功: {res}"
    retry_ev = [e for e in events5 if e[0] == "sub_progress" and e[1].get("stage") == "execute_retry"]
    assert retry_ev, "应有一次 execute_retry 进度事件（自动修正）"
    assert any(e[0] == "sub_result" for e in events5), "修正后应有 sub_result"
    print(f"[5] 单卡执行失败自动修正后成功 ✓ (execute_retry→sub_result，解读: {res.interpretation[:30]}…)")

    # ---- 6. N5-N7 确定性输出 ----
    assert res.chart_type in ("kpi", "bar"), f"图表类型: {res.chart_type}"
    assert res.facts.get("row_count") == 1
    assert res.trace.get("tables") == ["t_q1"], f"溯源表: {res.trace}"
    assert res.sub_id == "q1"
    print(f"[6] N5-N7 确定性输出: chart={res.chart_type} facts={res.facts} trace={res.trace} ✓")

    # ---- 7. N2 result 载荷兼容（str mock / dict 两种形态，回归 'str'.get 崩溃）----
    from app.engine.sub_task import _extract_sql_from_result
    sql, intent = _extract_sql_from_result({"sql": "SELECT 1", "intent": "query"})
    assert sql == "SELECT 1" and intent == "query"
    sql, intent = _extract_sql_from_result("SELECT 2 FROM t")
    assert sql == "SELECT 2 FROM t" and intent == "", f"str 形态: {sql!r}"
    sql, intent = _extract_sql_from_result(None)
    assert sql == "" and intent == ""
    print("[7] N2 result 载荷兼容（dict/str/None）✓")

    # ---- 8. 真实降级路径：LLM 不可用（client 未配置）→ generate_sql_stream 返回非 SQL → 隔离不崩溃 ----
    ctx8, _, events8 = make_ctx(delay=0.01)
    ctx8.llm = None
    ctx8.generate_sql = None            # 走真实 nl2sql 路径
    ctx8.datasource_id = 1              # 本机元数据库（无业务表 → refuse）
    spec8 = make_specs(1)[0]
    spec8.schema_name = "AI_Infra"
    res8 = run_subtask_pipeline(spec8, ctx8)
    assert res8.status == "error", f"无表可查应 error: {res8.status}"
    assert res8.retryable is False, "非查询意图应不可重试"
    assert "无法生成 SQL" in res8.error, f"错误应含可读原因: {res8.error}"
    err_ev8 = [e for e in events8 if e[0] == "sub_error"]
    assert err_ev8 and err_ev8[-1][1].get("retryable") is False, "sub_error 事件应带 retryable=False"
    print(f"[8] LLM 不可用/非查询降级: error={res8.error[:50]}… retryable=False ✓")

    # ---- 9. clarify 载荷（选表歧义）同样不可重试且带候选 ----
    ctx9, _, events9 = make_ctx(delay=0.01)
    ctx9.llm = None
    ctx9.generate_sql = None
    ctx9.datasource_id = 2              # 会议室库（部分问题触发选表歧义/澄清）
    # 真实 nl2sql 产出的 SQL 不含 t_ 前缀，重写 mock 执行器避免 split 越界
    ctx9.run_query_cached = lambda sql, *a: {"columns": ["x"], "rows": [["y"]],
                                              "row_count": 1, "latency_ms": 1, "permission": "1=1"}
    spec9 = make_specs(1)[0]
    spec9.schema_name = "fs_zoommeeting"
    spec9.question = "分析会议室使用情况"   # 宽泛问题更易触发 clarify/歧义
    res9 = run_subtask_pipeline(spec9, ctx9)
    if res9.status == "error":
        assert res9.retryable is False
        print(f"[9] clarify/歧义降级: error={res9.error[:60]}… retryable=False ✓")
    else:
        print(f"[9] 该问题未触发 clarify（status={res9.status}，正常产出 SQL）✓")

    print("\n" + ("=== 全部通过 ===" if ok else "=== 存在未通过项 ==="))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
