"""外键 ID 字段关联解析：根据关系图找到 ID 对应的关联表与展示名称字段，
避免 SQL 直接用 ID 做分组/展示维度。"""
from __future__ import annotations

import logging
import re

from ..database import SessionLocal
from ..models import ColumnMeta, Relationship, TableMeta

logger = logging.getLogger(__name__)

# 展示名称列的高优先匹配（列名精确或后缀）
_NAME_RE = re.compile(r"(?:^|_)(name|title|label)$", re.IGNORECASE)
# 注释中表示名称/标题的关键词
_COMMENT_NAME_RE = re.compile(r"名称|标题|名字|简称")
# 排除列（ID/编码/状态/时间等，不可能是展示名称）
_EXCLUDE_NAMES = {
    "id", "uuid", "code", "status", "type", "category", "level",
    "created_at", "updated_at", "create_time", "update_time",
    "start_time", "end_time", "deleted", "is_deleted", "enabled",
}
# 排除数据类型（大文本/JSON 不适合做展示维度）
_EXCLUDE_TYPES = {"text", "mediumtext", "longtext", "json", "blob"}


def _score_display_column(col: ColumnMeta) -> int:
    """给字段打分，分数越高越适合做关联表的展示名称列。"""
    name = (col.column_name or "").lower()
    cmt = col.comment or ""
    dtype = (col.data_type or "").lower()
    if name in _EXCLUDE_NAMES or dtype in _EXCLUDE_TYPES:
        return -100
    score = 0
    if _NAME_RE.search(name):
        score += 5
    if name.endswith("_name") or name.endswith("_title") or name.endswith("_label"):
        score += 3
    if "name" in name or "title" in name:
        score += 1
    if _COMMENT_NAME_RE.search(cmt):
        score += 4
    # 短字符串优先（varchar 长度适中）
    if dtype.startswith("varchar"):
        score += 1
    return score


def _find_display_column(db, table_meta_id: int) -> ColumnMeta | None:
    """在指定表中找到最适合做展示名称的字段。"""
    cols = db.query(ColumnMeta).filter(ColumnMeta.table_meta_id == table_meta_id).all()
    best: ColumnMeta | None = None
    best_score = 0
    for c in cols:
        s = _score_display_column(c)
        if s > best_score:
            best_score = s
            best = c
    return best


def get_id_display_hints(datasource_id: int, table_names: list[str] | None = None) -> list[dict]:
    """公共方法：查询指定数据源中，源表外键 ID 字段 → 关联表 + 展示名称字段 的映射。

    返回列表每项：
      src_table / src_col / dst_table / dst_col / display_col / display_comment / rel_type
    用于提示词中告知 LLM：遇到该 ID 字段时 JOIN 关联表，用 display_col 做展示。
    """
    db = SessionLocal()
    try:
        # 加载关系（仅启用）
        q = db.query(Relationship).filter(
            Relationship.datasource_id == datasource_id,
            Relationship.enabled.is_(True),
        )
        rels = q.all()
        if not rels:
            return []

        # 预加载涉及的表和列
        table_ids = set()
        col_ids = set()
        for r in rels:
            table_ids.update([r.src_table_id, r.dst_table_id])
            col_ids.update([r.src_col_id, r.dst_col_id])
        tmap = {t.id: t for t in db.query(TableMeta).filter(TableMeta.id.in_(table_ids)).all()}
        cmap = {c.id: c for c in db.query(ColumnMeta).filter(ColumnMeta.id.in_(col_ids)).all()}

        hints: list[dict] = []
        for r in rels:
            src_t = tmap.get(r.src_table_id)
            dst_t = tmap.get(r.dst_table_id)
            src_c = cmap.get(r.src_col_id)
            dst_c = cmap.get(r.dst_col_id)
            if not all([src_t, dst_t, src_c, dst_c]):
                continue
            if table_names and src_t.table_name not in table_names:
                continue
            # 在目标表中找展示名称列
            disp = _find_display_column(db, r.dst_table_id)
            hints.append({
                "src_table": src_t.table_name,
                "src_col": src_c.column_name,
                "dst_table": dst_t.table_name,
                "dst_col": dst_c.column_name,
                "display_col": disp.column_name if disp else "",
                "display_comment": disp.comment if disp else "",
                "rel_type": r.rel_type,
            })
        return hints
    finally:
        db.close()


def format_id_hints(hints: list[dict]) -> str:
    """将 ID 关联提示格式化为提示词文本。"""
    if not hints:
        return ""
    lines = []
    for h in hints:
        disp = h["display_col"]
        disp_info = f"{disp}({h['display_comment']})" if disp else "（未找到名称字段，可用目标表主键）"
        join = (f"{h['src_table']}.{h['src_col']} = {h['dst_table']}.{h['dst_col']}")
        lines.append(f"- {h['src_table']}.{h['src_col']} → JOIN {h['dst_table']} ON {join}，展示字段: {h['dst_table']}.{disp_info}")
    return "\n".join(lines)


# 噪声表关键词（备份/配置/快照/统计/黑白名单等，不适合作为主查询事实表）
_NOISE_NAME_RE = re.compile(
    r"(_bak|backup|_snapshot|_uat|_test|_tmp|_temp|_log$|white_list|black_list|"
    r"_callback_config|_processor_config|_global_config|_check_formula_change|"
    r"_month_statistics|_statistics$|_stats$|checklist$|_relation$|_mapping$|"
    r"_mark$|_condition$|_branch$|_processor_white_list|_seal_file|_seal_task|"
    r"_bill_relation|_preview_record|_ai_summary|_callback_record|_role_template|"
    r"_card_info|_card_list|_node_uat|_node_his|_node_bak|_node_condition_relation|"
    r"_summary$|_tag$|_flow_node|_flow_condition|_flow_branch|_flow_node)",
    re.IGNORECASE,
)


def narrow_candidate_tables(datasource_id: int, candidates: list[dict],
                             question: str = "", top_k: int = 8) -> list[dict]:
    """公共方法：用关系图 + 噪声过滤收窄候选表列表，避免返回过多表影响用户选择。

    评分规则：
    - 作为外键源（有 outgoing 关系）的表 → 事实表，+3
    - 作为外键目标（有 incoming 关系）的表 → 维度表，+1
    - 表名/注释命中问题核心词 → +2
    - 命中噪声表关键词 → 直接过滤
    返回按分数降序的 top_k 条。
    """
    if not candidates:
        return []
    # 加载关系，统计每张表的 outgoing/incoming 次数
    db = SessionLocal()
    try:
        rels = db.query(Relationship).filter(
            Relationship.datasource_id == datasource_id,
            Relationship.enabled.is_(True),
        ).all()
        table_ids = {r.src_table_id for r in rels} | {r.dst_table_id for r in rels}
        tmap = {t.id: t.table_name for t in db.query(TableMeta).filter(TableMeta.id.in_(table_ids)).all()}
    finally:
        db.close()

    outgoing: dict[str, int] = {}
    incoming: dict[str, int] = {}
    for r in rels:
        src = tmap.get(r.src_table_id)
        dst = tmap.get(r.dst_table_id)
        if src:
            outgoing[src] = outgoing.get(src, 0) + 1
        if dst:
            incoming[dst] = incoming.get(dst, 0) + 1

    # 问题核心词（取 2-4 字的中文词，用于表名/注释匹配）
    q_words = set(re.findall(r"[\u4e00-\u9fa5]{2,4}", question or ""))

    scored: list[tuple[float, dict]] = []
    for c in candidates:
        tname = c.get("table", "")
        comment = c.get("comment", "") or ""
        if not tname:
            continue
        # 噪声表直接过滤
        if _NOISE_NAME_RE.search(tname):
            continue
        score = 0.0
        score += outgoing.get(tname, 0) * 3
        score += incoming.get(tname, 0) * 1
        # 问题核心词命中表名或注释
        for w in q_words:
            if w and (w in tname or w in comment):
                score += 2
                break
        scored.append((score, c))

    # 按分数降序，同分保持原顺序
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:top_k]]
