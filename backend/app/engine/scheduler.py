"""定时任务引擎（简化实现）：cron 表达式解析 + 后台线程调度 + 参数解析。
cron 支持格式：daily HH:MM / weekly W HH:MM / hourly / interval SECONDS / "每天 09:00"。
生产环境可替换为 APScheduler / ARQ 定时（见 HLD 13.1 待确认项）。"""
import logging
import threading
import time
from datetime import datetime, timedelta

from ..database import SessionLocal
from ..models import ScheduledTask

logger = logging.getLogger(__name__)


class CronParseError(Exception):
    pass


def parse_cron(expr: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    e = expr.strip().lower()
    if e.startswith("interval "):
        secs = int(e.split()[1])
        return now + timedelta(seconds=secs)
    if e == "hourly":
        return now.replace(minute=0, second=0) + timedelta(hours=1)
    if e.startswith("daily"):
        parts = e.split()
        hm = parts[1] if len(parts) > 1 else "09:00"
        h, m = map(int, hm.split(":"))
        nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        return nxt
    if e.startswith("weekly"):
        parts = e.split()
        weekday = int(parts[1])  # 0=周一
        hm = parts[2] if len(parts) > 2 else "09:00"
        h, m = map(int, hm.split(":"))
        days = (weekday - now.weekday()) % 7
        nxt = (now + timedelta(days=days)).replace(hour=h, minute=m, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=7)
        return nxt
    raise CronParseError(f"不支持的 cron 表达式: {expr}")


def _resolve_params(task: ScheduledTask) -> dict:
    """参数解析：固定值直接使用；T-1 等动态表达式按运行时间计算。"""
    values = dict(task.param_values_json or {})
    today = datetime.now().date()
    for key, val in values.items():
        if isinstance(val, str):
            s = val.strip().lower()
            if s == "t-1":
                values[key] = (today - timedelta(days=1)).isoformat()
            elif s == "today":
                values[key] = today.isoformat()
            elif s == "month_start":
                values[key] = today.replace(day=1).isoformat()
    return values


def _replace_params(sql: str, params: dict, param_types: dict[str, str] | None = None) -> str:
    out = sql
    for key, val in params.items():
        ptype = (param_types or {}).get(key, "string")
        if ptype in ("number", "int", "float"):
            out = out.replace("{" + key + "}", str(val))
        else:
            escaped = str(val).replace("'", "''")
            out = out.replace("{" + key + "}", f"'{escaped}'")
    return out


def run_task(task_id: int) -> dict:
    """执行一个定时任务：参数解析 → 权限重校验 → 执行 → 图表快照入库。"""
    from ..executor import run_query
    from ..models import SavedQuery, TaskRunLog
    db = SessionLocal()
    start = time.time()
    try:
        task = db.query(ScheduledTask).get(task_id)
        if not task:
            return {"ok": False, "error": "任务不存在"}
        sq = db.query(SavedQuery).get(task.saved_query_id)
        if not sq:
            return {"ok": False, "error": "保存查询不存在"}
        params = _resolve_params(task)
        param_types = {p.get("key", ""): p.get("type", "string")
                       for p in (sq.params_json or [])}
        sql = _replace_params(sq.sql_text, params, param_types)
        ds_id = _datasource_id_of(sql)
        user_id = sq.owner_id
        log = TaskRunLog(task_id=task_id, param_values_json=params, status="running")
        db.add(log)
        db.commit()
        try:
            result = run_query(ds_id, sql, user_id)
            from ..engine.chart import build_chart_option
            chart = build_chart_option(result["columns"], result["rows"])
            log.status = "success"
            log.row_count = result["row_count"]
            log.chart_snapshot_json = {"columns": result["columns"], "rows": result["rows"],
                                       "chart": chart}
            log.latency_ms = result["latency_ms"]
            task.last_run_at = datetime.utcnow()
            task.next_run_at = parse_cron(task.cron_expr)
            db.commit()
            return {"ok": True, "row_count": result["row_count"], "chart": chart}
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            log = db.query(TaskRunLog).get(log.id)
            log.status = "failed"
            log.error_msg = str(exc)[:500]
            log.latency_ms = int((time.time() - start) * 1000)
            db.commit()
            return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def _datasource_id_of(sql: str) -> int:
    """从 SQL 中提取表名并反查数据源（简化：取第一张表）。"""
    from ..engine.nl2sql import retrieve_candidate_tables  # noqa: F401
    import re
    from ..models import TableMeta
    m = re.search(r"from\s+([`\"\[]?)([\w]+)", sql, re.IGNORECASE)
    if not m:
        raise CronParseError("SQL 未包含表名")
    table_name = m.group(2)
    db = SessionLocal()
    try:
        tm = db.query(TableMeta).filter(TableMeta.table_name == table_name).first()
        return tm.datasource_id if tm else 0
    finally:
        db.close()


class SchedulerThread(threading.Thread):
    """单机后台调度线程：每 30s 扫描到期任务。"""

    def __init__(self):
        super().__init__(daemon=True)
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                db = SessionLocal()
                try:
                    tasks = (db.query(ScheduledTask)
                             .filter(ScheduledTask.status == "enabled").all())
                    now = datetime.now()
                    for task in tasks:
                        if task.next_run_at is None:
                            task.next_run_at = parse_cron(task.cron_expr)
                            db.commit()
                            continue
                        if task.next_run_at <= now:
                            logger.info("触发定时任务 %s", task.name)
                            try:
                                run_task(task.id)
                            except Exception as exc:  # noqa: BLE001
                                logger.error("定时任务失败: %s", exc)
                            db.refresh(task)
                            task.next_run_at = parse_cron(task.cron_expr)
                            db.commit()
                finally:
                    db.close()
            except Exception as exc:  # noqa: BLE001
                logger.error("调度循环异常: %s", exc)
            self._stop.wait(30)

    def stop(self):
        self._stop.set()


_scheduler: SchedulerThread | None = None


def start_scheduler():
    global _scheduler
    if _scheduler is None or not _scheduler.is_alive():
        _scheduler = SchedulerThread()
        _scheduler.start()
        logger.info("定时任务调度器已启动")


def stop_scheduler():
    """停止调度线程（应用 shutdown 时调用），最多等待 5 秒。"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.stop()
        _scheduler.join(timeout=5)
        _scheduler = None
        logger.info("定时任务调度器已停止")
