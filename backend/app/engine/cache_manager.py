"""C1 · SQL/结果缓存：一级生成缓存（相似问句复用 SQL）+ 二级结果缓存（SQL 指纹）。

缓存合同（对齐 cache-contract）：
- identity：workspace_id + cache_type + cache_key 唯一；一级键=归一化问句，二级键=SQL 指纹
- representation：payload_json 存生成结果/结果集；expires_at 表示新鲜度；schema_version 记录生成时 Schema 版本
- freshness：TTL（gen 7d / result 15min），由时间与主动失效共同控制
- read：命中返回 payload；未命中返回 None（缓存不承担"真值不存在"语义）
- change：Schema 同步 / 权限规则变更 → 主动清空对应 scope 缓存
- failure：读写失败一律吞掉并记日志，不影响主链路
"""
import hashlib
import logging
from datetime import datetime, timedelta

import sqlglot

from ..config_override import get_effective as get_settings
from ..executor import to_jsonable

logger = logging.getLogger(__name__)

GEN_TTL = "gen"
RESULT_TTL = "result"


# ---------- 键与相似度 ----------

def normalize_question(q: str) -> str:
    """归一化问句：去空白/标点、小写。"""
    import re

    text = re.sub(r"[\s，。？！、；：（）【】《》“”‘’,.?!;:()<>-]+", "", (q or "").strip().lower())
    return text


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def text_similarity(a: str, b: str) -> float:
    """中文问句相似度：字符 bigram Dice(0.6) + 词 Dice(0.4) 加权。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sa, sb = normalize_question(a), normalize_question(b)
    if not sa or not sb:
        return 0.0
    ba, bb = _bigrams(sa), _bigrams(sb)
    bigram_dice = 2 * len(ba & bb) / (len(ba) + len(bb)) if (ba or bb) else 0.0
    wa, wb = set(sa), set(sb)
    word_dice = 2 * len(wa & wb) / (len(wa) + len(wb)) if (wa or wb) else 0.0
    return round(0.6 * bigram_dice + 0.4 * word_dice, 4)


def fingerprint_sql(sql: str, dialect: str = "mysql") -> str:
    """SQL 指纹：解析 → 规范方言输出（脱字面量可选）→ sha256 前 40 位。"""
    try:
        ast = sqlglot.parse_one(sql, read=dialect)
        canonical = ast.sql(dialect="mysql")
    except Exception:  # noqa: BLE001
        canonical = (sql or "").strip()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:40]


def schema_version(datasource_id: int, db) -> datetime | None:
    """当前 Schema 同步版本：数据源各表 synced_at 最大值，无则 None。"""
    from ..models import TableMeta

    from sqlalchemy import func

    return (db.query(func.max(TableMeta.synced_at))
            .filter(TableMeta.datasource_id == datasource_id).scalar())


# ---------- 读写 ----------

def _now() -> datetime:
    return datetime.utcnow()


def _expires(ttl_sec: int) -> datetime:
    return _now() + timedelta(seconds=ttl_sec)


def _evict_overflow(ws_id: int, db) -> None:
    """缓存条目数超限时删除最旧条目（max_entries_per_ws）。"""
    from ..models import CacheEntry

    settings = get_settings()
    total = db.query(CacheEntry).filter(CacheEntry.workspace_id == ws_id).count()
    if total <= settings.cache_max_entries_per_ws:
        return
    overflow = total - settings.cache_max_entries_per_ws
    stale = (db.query(CacheEntry)
             .filter(CacheEntry.workspace_id == ws_id)
             .order_by(CacheEntry.expires_at.asc(), CacheEntry.last_hit_at.asc())
             .limit(overflow).all())
    for c in stale:
        db.delete(c)
    logger.info("[cache] 超限清理 %d 条 (ws=%s)", len(stale), ws_id)


def _payload_of(entry) -> dict:
    try:
        return entry.payload_json or {}
    except Exception:  # noqa: BLE001
        return {}


# ---- 二级结果缓存 ----

def lookup_result_cache(cache_key: str, ws_id: int, datasource_id: int,
                        db) -> dict | None:
    """结果缓存读取。命中返回 JSON 化后的 exec_result；未命中/过期/版本不符返回 None。"""
    from ..models import CacheEntry

    settings = get_settings()
    if not settings.cache_enabled:
        return None
    entry = (db.query(CacheEntry)
             .filter(CacheEntry.workspace_id == ws_id,
                     CacheEntry.cache_type == RESULT_TTL,
                     CacheEntry.cache_key == cache_key)
             .first())
    if entry is None:
        return None
    now = _now()
    if entry.expires_at is None or entry.expires_at < now:
        db.delete(entry)
        return None
    if entry.schema_version and entry.schema_version != schema_version(datasource_id, db):
        db.delete(entry)  # Schema 已变，结果可能失效
        return None
    entry.hit_count = (entry.hit_count or 0) + 1
    entry.last_hit_at = now
    return _payload_of(entry)


def store_result_cache(cache_key: str, sql: str, ws_id: int, datasource_id: int,
                       exec_result: dict, db) -> None:
    """结果缓存写入（幂等 upsert）。rows 先 JSON 化。"""
    from ..models import CacheEntry

    settings = get_settings()
    if not settings.cache_enabled:
        return
    try:
        payload = dict(exec_result)
        payload["rows"] = to_jsonable(exec_result.get("rows") or [])
        entry = (db.query(CacheEntry)
                 .filter(CacheEntry.workspace_id == ws_id,
                         CacheEntry.cache_type == RESULT_TTL,
                         CacheEntry.cache_key == cache_key)
                 .first())
        if entry is None:
            entry = CacheEntry(workspace_id=ws_id, cache_type=RESULT_TTL,
                               cache_key=cache_key, sql_text=sql,
                               datasource_id=datasource_id)
            db.add(entry)
        entry.payload_json = payload
        entry.sql_text = sql
        entry.datasource_id = datasource_id
        entry.schema_version = schema_version(datasource_id, db)
        entry.expires_at = _expires(settings.cache_ttl_result_sec)
        _evict_overflow(ws_id, db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cache] 结果缓存写入失败: %s", exc)


# ---- 一级生成缓存 ----

def lookup_gen_cache(question: str, ws_id: int, datasource_id: int, db) -> dict | None:
    """生成缓存读取：先归一化精确匹配，再相似问句检索（阈值 cache_similarity）。"""
    from ..models import CacheEntry

    settings = get_settings()
    if not settings.cache_enabled:
        return None
    nq = normalize_question(question)
    if not nq:
        return None
    entry = (db.query(CacheEntry)
             .filter(CacheEntry.workspace_id == ws_id,
                     CacheEntry.cache_type == GEN_TTL,
                     CacheEntry.datasource_id == datasource_id)
             .order_by(CacheEntry.id.desc())
             .limit(200).all())
    now = _now()
    best: tuple[float, CacheEntry] | None = None
    for e in entry:
        if e.expires_at and e.expires_at < now:
            continue
        eq = normalize_question(e.question or "")
        if not eq:
            continue
        if eq == nq:
            best = (1.0, e)
            break
        sim = text_similarity(nq, eq)
        if sim > settings.cache_similarity and (best is None or sim > best[0]):
            best = (sim, e)
    if best is None:
        return None
    hit = best[1]
    if hit.schema_version and hit.schema_version != schema_version(datasource_id, db):
        return None
    hit.hit_count = (hit.hit_count or 0) + 1
    hit.last_hit_at = now
    return {"sql": hit.sql_text or "", "_cache_key": hit.cache_key}


def store_gen_cache(question: str, sql: str, ws_id: int, datasource_id: int, db) -> str | None:
    """生成缓存写入，返回缓存键（便于 QueryLog 记录）。"""
    from ..models import CacheEntry

    settings = get_settings()
    if not settings.cache_enabled:
        return None
    try:
        nq = normalize_question(question)
        if not nq or not (sql or "").strip():
            return None
        cache_key = hashlib.sha256(f"gen:{ws_id}:{datasource_id}:{nq}".encode()).hexdigest()[:40]
        entry = (db.query(CacheEntry)
                 .filter(CacheEntry.workspace_id == ws_id,
                         CacheEntry.cache_type == GEN_TTL,
                         CacheEntry.cache_key == cache_key)
                 .first())
        if entry is None:
            entry = CacheEntry(workspace_id=ws_id, cache_type=GEN_TTL,
                               cache_key=cache_key, question=question,
                               datasource_id=datasource_id)
            db.add(entry)
        entry.question = question
        entry.sql_text = sql
        entry.datasource_id = datasource_id
        entry.schema_version = schema_version(datasource_id, db)
        entry.expires_at = _expires(settings.cache_ttl_gen_sec)
        _evict_overflow(ws_id, db)
        return cache_key
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cache] 生成缓存写入失败: %s", exc)
        return None


# ---- 主动失效 ----

def invalidate_for_datasource(datasource_id: int, db) -> int:
    """Schema 同步后失效该数据源全部缓存。返回删除条数。"""
    from ..models import CacheEntry

    rows = db.query(CacheEntry).filter(CacheEntry.datasource_id == datasource_id).all()
    n = len(rows)
    for r in rows:
        db.delete(r)
    if n:
        logger.info("[cache] 数据源 %s 缓存失效 %d 条", datasource_id, n)
    return n


def invalidate_all_for_workspace(ws_id: int, db) -> int:
    """权限规则变更后失效该工作空间全部缓存（结果可见性可能已变）。"""
    from ..models import CacheEntry

    rows = db.query(CacheEntry).filter(CacheEntry.workspace_id == ws_id).all()
    n = len(rows)
    for r in rows:
        db.delete(r)
    if n:
        logger.info("[cache] workspace %s 缓存失效 %d 条", ws_id, n)
    return n
