"""洞察分析定时任务调度器：按CRON表达式定时执行洞察分析，结果保存至报告中心。"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from ..database import SessionLocal
from ..models import InsightSchedule


def parse_cron(expr: str, now: datetime | None = None) -> datetime:
    """简化版CRON解析：支持 分 时 日 月 周，* 表示任意。"""
    if now is None:
        now = datetime.now()
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"CRON表达式必须包含5个字段（分 时 日 月 周），当前: {expr}")
    minute, hour, day, month, weekday = parts

    def _field(val: str, lo: int, hi: int) -> list[int]:
        if val == "*":
            return list(range(lo, hi + 1))
        if "," in val:
            result = []
            for v in val.split(","):
                result.extend(_field(v, lo, hi))
            return sorted(set(result))
        if "/" in val:
            base, step = val.split("/")
            nums = _field(base, lo, hi)
            return [n for n in nums if n % int(step) == 0]
        if "-" in val:
            a, b = val.split("-")
            return list(range(int(a), int(b) + 1))
        return [int(val)]

    minutes = _field(minute, 0, 59)
    hours = _field(hour, 0, 23)
    days = _field(day, 1, 31)
    months = _field(month, 1, 12)
    weekdays = _field(weekday, 0, 6)  # 0=周日

    # 从下一分钟开始查找下一个匹配时间
    candidate = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):  # 最多搜索1年
        if (candidate.minute in minutes and candidate.hour in hours
                and candidate.day in days and candidate.month in months
                and candidate.weekday() in weekdays):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError(f"无法在1年内找到匹配的CRON时间: {expr}")


def run_insight_schedule(schedule_id: int) -> dict:
    """执行一个洞察定时任务：调用InsightGenerator.execute，结果保存至报告中心。"""
    from ..engine.insight_generator import InsightGenerator, InsightConfig
    from ..engine.llm_provider import resolve_llm_client, resolve_llm_client_by_id

    db = SessionLocal()
    try:
        sched = db.query(InsightSchedule).get(schedule_id)
        if not sched:
            return {"ok": False, "error": "定时任务不存在"}
        if sched.status != "active":
            return {"ok": False, "error": "任务已暂停"}

        config = InsightConfig.from_dict(sched.config_snapshot or {})

        # 解析LLM客户端（与AI问数共用方法）
        if sched.model_id:
            llm = resolve_llm_client_by_id(db, sched.model_id, sched.workspace_id)
        else:
            llm = resolve_llm_client(db, sched.workspace_id, scene="sql")

        gen = InsightGenerator(db, llm=llm, workspace_id=sched.workspace_id)

        # 执行洞察分析（与手动执行共用同一方法），结果自动写入report表
        report = gen.execute(
            config,
            template_id=sched.template_id,
            prompt_template_id=sched.prompt_template_id,
            created_by=sched.created_by,
        )

        # 更新任务状态
        sched.last_run_at = datetime.now()
        sched.last_report_id = report.id
        sched.next_run_at = parse_cron(sched.cron_expr)
        sched.run_count = (sched.run_count or 0) + 1
        db.commit()

        return {"ok": True, "report_id": report.id, "title": report.title}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return {"ok": False, "error": str(exc)[:500]}
    finally:
        db.close()


class InsightSchedulerThread(threading.Thread):
    """后台线程：每分钟检查到期的洞察定时任务并执行。"""
    daemon = True

    def __init__(self):
        super().__init__(name="InsightScheduler")
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                db = SessionLocal()
                now = datetime.now()
                tasks = db.query(InsightSchedule).filter(
                    InsightSchedule.status == "active",
                    InsightSchedule.next_run_at <= now,
                ).all()
                db.close()
                for t in tasks:
                    run_insight_schedule(t.id)
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(60)  # 每分钟检查一次

    def stop(self):
        self._stop.set()


_scheduler: InsightSchedulerThread | None = None


def start_insight_scheduler():
    global _scheduler
    if _scheduler is None or not _scheduler.is_alive():
        _scheduler = InsightSchedulerThread()
        _scheduler.start()


def stop_insight_scheduler():
    global _scheduler
    if _scheduler:
        _scheduler.stop()
        _scheduler = None
