"""NL2SQL 引擎：意图识别 → Schema/RAG 检索 → Prompt 组装 → LLM 生成 → 校验/自纠正。

链路说明（与需求一致）：
1. 意图识别：LLM 输出 intent（query / refuse / chat），无关问题直接拒绝；
2. 召回：关键词候选表 + JOIN 路径 + 向量 few-shot（FAQ）+ 知识库文档 RAG 片段；
3. 生成：LLM 依据 Schema + JOIN + few-shot + 文档上下文生成 SQL（JSON）；
4. 校验：sqlglot 语法/方言校验失败自动回灌重试 ≤ retries 次。
未配置有效 LLM 时进入 mock 演示模式（结果标注"演示模式"）。
"""
import json
import logging
import re
import time
from typing import Any

from ..database import SessionLocal
from ..llm import LLMClient, LLMError, parse_json_content
from ..models import ColumnMeta, Datasource, Relationship, SqlExample, TableMeta
from .text_utils import tokenize  # L-0 公共化：统一分词
from .schema_types import SYSTEM_TABLES, is_id_field, is_system_field  # L-0 公共化：类型/系统字段
from .biz_lexicon import match as lex_match  # L-0 公共化：业务词表
from . import prompt_kit  # L-0 公共化：prompt 拼装
from .prompt_service import PromptService  # 线上可编辑：sql_generation 场景默认模板
from .llm_json import extract_json as llm_extract_json  # L-0 公共化：LLM JSON 解析

# 向后兼容：旧引用 SYSTEM_TABLE_BLACKLIST 的模块
SYSTEM_TABLE_BLACKLIST = SYSTEM_TABLES

logger = logging.getLogger(__name__)


_NON_SQL_INTENT_RE = re.compile(
    r"intent\s*[:：=]\s*[\"']?(chat|knowledge|refuse)[\"']?|"
    r"(?:无法生成|无法回答|无法提供|未检索到相关表|未检索到相关字段|不属于数据查询|不是数据查询|不涉及数据库|与数据库无关|"
    r"无需查询|不用查询数据库|不需要查询数据库|拒绝回答|不合法请求|没有相关数据|无相关数据)",
    re.IGNORECASE)


def _detect_non_sql_answer(content: str) -> str | None:
    """模型按判定规则判断为非数据查询时（intent=chat/knowledge/refuse），
    返回对应 intent；仍像 SQL 生成失败的回答返回 None（走重试）。"""
    if not content:
        return None
    m = _NON_SQL_INTENT_RE.search(content)
    if not m:
        return None
    for g in m.groups():
        if g in ("chat", "knowledge", "refuse"):
            return g
    return "chat"


# 输出协议标记：模型按「五、判定规则」写在首行的 intent=xxx 标记（落库/展示前剥离）
_INTENT_MARKER_RE = re.compile(
    r"^\s*intent\s*[:：=]\s*[\"']?(chat|knowledge|refuse|query)[\"']?[ \t]*\r?\n?",
    re.IGNORECASE)


def _strip_intent_marker(content: str) -> str:
    """剥离首行 intent=xxx 协议标记，返回可展示的自然语言正文。"""
    if not content:
        return content
    return _INTENT_MARKER_RE.sub("", content.strip(), count=1).strip()


# 相对时间表达（近7天/本月/昨天等）：命中时 SQL 必须参数化，禁止写死固定日期
_TIME_RELATIVE_RE = re.compile(
    r"(?:近|最近|过去|前)\s*\d{1,3}\s*(?:天|日|周|个?月)"
    r"|(?:上|本|这|当|今|下)\s*(?:个?月|个?季度|季度|季)"
    r"|(?:去|今|本|这|明)\s*年"
    r"|昨天|今天|前天|上周|本周|下周|上月|本月|下月"
)


def _is_relative_time_expr(expr: str) -> bool:
    return bool(expr and _TIME_RELATIVE_RE.search(expr))


# 系统提示词单一事实源：统一由 prompt_kit 分段拼装，禁止在本文件内分叉维护
SYSTEM_PROMPT = prompt_kit.build_sql_system_prompt()

# 线上可编辑：优先读取 PromptService 中 scene_type=sql_generation 的默认模板；
# 未配置/读取失败/异常时回退到内置默认（prompt_kit.build_sql_system_prompt()）。
_SQL_PROMPT_CACHE: dict[str, Any] = {"t": 0.0, "text": None}
_SQL_PROMPT_TTL = 15.0  # 秒：管理员在线编辑模板后最多 15s 生效，同时避免每请求查库


def _load_sql_system_prompt() -> str:
    """运行时获取 SQL 生成系统提示词：线上模板优先，未配置走内置默认。"""
    now = time.monotonic()
    cached = _SQL_PROMPT_CACHE
    if cached["text"] is not None and now - cached["t"] < _SQL_PROMPT_TTL:
        return cached["text"]
    text = SYSTEM_PROMPT
    try:
        db = SessionLocal()
        try:
            svc = PromptService(db, workspace_id=0)  # SQL 系统提示词为全局生成逻辑，取全局默认
            tpl = svc.get_default("sql_generation")
            if tpl and tpl.prompt_template and tpl.prompt_template.strip():
                text = tpl.prompt_template
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("加载 sql_generation 线上模板失败，回退内置默认: %s", exc)
    _SQL_PROMPT_CACHE.update(t=now, text=text)
    return text


def _schema_text(datasource_id: int, top_tables: list[TableMeta] | None = None,
                 schema_name: str | None = None,
                 preferred: set[str] | None = None) -> str:
    """Step3+4：按选中表 ID 一条 IN 查询 column_meta，关联回原表，按用户口径格式化：
    `表名: 列(类型·注释), 列(类型·注释)`（每表一行，全量字段不裁剪，按 ordinal 排序）。
    preferred 字段（spec 已映射/检索命中的目标字段）加 ★ 前缀，帮助 LLM 聚焦。"""
    db = SessionLocal()
    try:
        if top_tables:
            tables = top_tables
        else:
            q = (db.query(TableMeta)
                 .filter(TableMeta.datasource_id == datasource_id,
                         TableMeta.deprecated.is_(False)))
            if schema_name:
                q = q.filter(TableMeta.schema_name == schema_name)
            tables = q.all()
        if not tables:
            return ""
        # Step3：一条 IN 查询全部选中表的字段（替代逐表 N 次查询）
        table_ids = [t.id for t in tables]
        all_cols = (db.query(ColumnMeta)
                    .filter(ColumnMeta.table_meta_id.in_(table_ids))
                    .order_by(ColumnMeta.ordinal).all())
        cols_by_table: dict[int, list[ColumnMeta]] = {}
        for c in all_cols:
            cols_by_table.setdefault(c.table_meta_id, []).append(c)
        preferred = preferred or set()
        lines = []
        for t in tables:  # Step4：关联回原表，按用户锁定格式输出
            parts = []
            for c in cols_by_table.get(t.id, []):
                seg = c.column_name
                type_or_comment = "·".join(x for x in (c.data_type, c.comment) if x)
                if type_or_comment:
                    seg += f"({type_or_comment})"
                # C7 样例值注入：低基数字段追加 <样例值>，帮助 LLM 使用准确枚举值
                try:
                    samples = (c.samples_json or [])[:3]
                except Exception:  # noqa: BLE001
                    samples = []
                vals = " / ".join(str(s.get("value")) for s in samples
                                  if s and s.get("value") is not None)
                if vals:
                    seg += f"<{vals}>"
                if c.column_name in preferred:
                    seg = f"★{seg}"  # spec 已映射/检索命中的目标字段，优先使用
                parts.append(seg)
            lines.append(f"{t.table_name}: {', '.join(parts)}")
        schema_body = "\n".join(lines)
        # 追加外键 ID → 关联表.名称字段 的映射提示，引导 LLM 用名称替代 ID 做展示
        try:
            from .id_resolver import format_id_hints, get_id_display_hints
            table_names = [t.table_name for t in tables]
            hints = get_id_display_hints(datasource_id, table_names=table_names)
            hint_text = format_id_hints(hints)
        except Exception:  # noqa: BLE001
            hint_text = ""
        if hint_text:
            schema_body += "\n\n【ID 关联展示（外键字段 → 关联表.名称字段，遇到该 ID 时 JOIN 关联表并用名称字段做展示/分组，禁止直接用 ID 值做维度）】\n" + hint_text
        return schema_body
    finally:
        db.close()


def _join_paths_text(datasource_id: int,
                      tables: list[TableMeta] | None = None) -> str:
    """Step5：表关系文本。V1.1 双向查询——src_table_id IN 选中表 OR dst_table_id IN 选中表
    （任一端命中即输出，帮助 LLM 发现需要补关联的表）。"""
    db = SessionLocal()
    try:
        q = (db.query(Relationship)
             .filter(Relationship.datasource_id == datasource_id,
                     Relationship.enabled.is_(True)))
        rels = q.all()
        if tables:
            keep = {t.id for t in tables}
            rels = [r for r in rels
                    if r.src_table_id in keep or r.dst_table_id in keep]
        if not rels:
            return "（无）"
        out = []
        col_cache: dict[tuple[int, int], str] = {}

        def col_name(tid: int, cid: int) -> str:
            key = (tid, cid)
            if key not in col_cache:
                cm = db.query(ColumnMeta).get(cid)
                col_cache[key] = cm.column_name if cm else str(cid)
            return col_cache[key]

        for r in rels:
            src_t = db.query(TableMeta).get(r.src_table_id)
            dst_t = db.query(TableMeta).get(r.dst_table_id)
            if not src_t or not dst_t:
                continue
            rel = f"（{r.rel_type or ''}/{r.source or ''}）" if (r.rel_type or r.source) else ""
            out.append(f"{src_t.table_name}.{col_name(r.src_table_id, r.src_col_id)}"
                       f" → {dst_t.table_name}.{col_name(r.dst_table_id, r.dst_col_id)}{rel}")
        return "; ".join(out) if out else "（无）"
    finally:
        db.close()


def _tokenize_cn(question: str) -> set[str]:
    """Backward-compatible: delegates to text_utils.tokenize (L-0 public module)."""
    return tokenize(question)


def retrieve_candidate_tables(datasource_id: int, question: str, top_k: int = 12,
                              schema_name: str | None = None) -> list[TableMeta]:
    """关键词（表名/注释/字段注释）召回候选表；schema_name 非空时限定 schema 范围。"""
    db = SessionLocal()
    try:
        q = (db.query(TableMeta)
             .filter(TableMeta.datasource_id == datasource_id,
                     TableMeta.deprecated.is_(False))
             .filter(~TableMeta.table_name.in_(SYSTEM_TABLE_BLACKLIST)))
        if schema_name:
            q = q.filter(TableMeta.schema_name == schema_name)
        tables = q.all()
        q = question.lower()
        words = _tokenize_cn(q)
        # 批量查询所有表的字段（消除 N+1）
        table_ids = [t.id for t in tables]
        all_cols = (db.query(ColumnMeta)
                    .filter(ColumnMeta.table_meta_id.in_(table_ids)).all()) if table_ids else []
        cols_by_table: dict[int, list] = {}
        for c in all_cols:
            cols_by_table.setdefault(c.table_meta_id, []).append(c)
        scored = []
        for t in tables:
            score = 0
            name = t.table_name.lower()
            comment = (t.comment or "").lower()
            if name in words:
                score += 30
            if name in q:
                score += 10
            for w in words:
                if w in name or name in w:
                    score += 2
                if w in comment or comment in w:
                    score += 3
            for c in cols_by_table.get(t.id, []):
                cn, cc = c.column_name.lower(), (c.comment or "").lower()
                for w in words:
                    if len(w) > 1 and (w in cn or w in cc):
                        score += 1
            if score > 0:
                scored.append((score, t))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [t for _, t in scored[:top_k]]
    finally:
        db.close()


class TableSelectError(Exception):
    """两阶段选表失败（LLM 无法识别相关表），上层转为明确提示/澄清，不静默猜表。"""


# 选表 LLM 的 system prompt（输出协议与 _parse_select_blob 对齐：JSON 数组，空数组合法）
SELECT_TABLE_SYSTEM = """你是严格的数据库表选择器。任务：根据用户的数据查询问题，从【数据表清单】中选出本次查询需要的全部数据表。

【选择规则（按优先级）】
1. 相关性证据优先级：【用户查询目标】/【文件大小类字段命中】等字段命中证据块 > 表中文注释 > 表名；表注释为空时只能依据字段命中判断，禁止仅凭表名猜测；
2. 选表范围：主表 + 完成查询所必需的关联链路表（如按项目名称过滤需选出项目表；统计接口调用次数需选出执行记录表及其关联对象表）；关联链路只选【JOIN 路径】能走通所必需的表，不扩散；
3. 只选必需的表，与问题无关的表一律不选；多张语义相近表时选与问题最相关、字段命中最多的一张，不要全选；
4. 多轮追问：必须结合【历史对话】理解省略式/指代式追问（如“环比去年呢”“那每个接口呢”），沿用上一轮已确认的表；若历史中用户已明确点选确认某表，必须锁定该表、不得更换或遗漏；
5. 确实没有任何相关表时，输出空数组 []（这是合法结果，表示应走非数据查询/澄清，不要硬凑）。

【输出协议（必须严格遵守，否则下游无法解析）】
1. 只输出一个 JSON 数组，禁止输出 Markdown、代码块标记、解释文字、推理过程；
2. 每个元素结构固定为 {"id": 表ID整数, "table_name": "表名", "comment": "表注释"}，三个键名不得改变；
3. id 与 table_name 必须逐字复制自【数据表清单】，禁止编造清单中不存在的表或 ID；comment 同样逐字复制（无注释则为空字符串）；
4. 无相关表时输出 []。
正确示例：[{"id": 12, "table_name": "test_cases", "comment": "功能测试用例表"}]
错误示例（禁止）：用自然语言说明、输出 {"tables": [...]} 以外的对象包裹、id 写成表名字符串、输出清单里没有的表。"""


def _all_business_tables(db, datasource_id: int, schema_name: str | None):
    """Step2：全量业务表（无 LIMIT，排除 AI_Infra 系统表黑名单），按表名排序。"""
    q = (db.query(TableMeta)
         .filter(TableMeta.datasource_id == datasource_id,
                 TableMeta.deprecated.is_(False))
         .filter(~TableMeta.table_name.in_(SYSTEM_TABLE_BLACKLIST)))
    if schema_name:
        q = q.filter(TableMeta.schema_name == schema_name)
    return q.order_by(TableMeta.table_name).all()


def _hint_matches(blob: str, hint: str) -> bool:
    """拆表检索词匹配（参考实现 LIKE %kw%）：子串命中即视为匹配。"""
    return bool(hint) and hint.lower() in blob


def _hint_hit_index(table_name: str, comment: str, hints: list[str]) -> int:
    """返回首个命中检索词的索引（未命中返回 99），供候选排序。"""
    blob = f"{table_name} {comment or ''}".lower()
    for i, h in enumerate(hints):
        if h and _hint_matches(blob, h):
            return i
    return 99


def _parse_confirmed_tables(question: str, history: list[dict] | None) -> list[str]:
    """多轮澄清回填：解析「已确认查询表：A、B」（前端点选候选表后发送的确认消息）。"""
    texts = [question or ""]
    for m in (history or [])[-3:]:
        if m.get("role") == "user":
            texts.append(m.get("text", "") or "")
    names: list[str] = []
    for txt in texts:
        m = re.search(r"已确认查询表[:：]\s*(.+)", txt)
        if not m:
            continue
        for part in re.split(r"[、,，\s]+", m.group(1).strip()):
            part = part.strip(" ；;。.")
            if part and part not in names:
                names.append(part)
    return names


def _parse_select_blob(content: str) -> list[dict]:
    """解析选表 LLM 输出：主格式 [{id,table_name,comment}]；兼容 {"tables":[...]} 与 ["表名"]。"""
    blob = (content or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", blob, re.S)
    if m:
        blob = m.group(1).strip()
    # 优先按数组解析
    s, e = blob.find("["), blob.rfind("]")
    arr = None
    if s != -1 and e > s:
        try:
            arr = json.loads(blob[s:e + 1])
        except Exception:  # noqa: BLE001
            arr = None
    if arr is None:
        # 兼容 {"tables": [...]}
        oa, ob = blob.find("{"), blob.rfind("}")
        if oa != -1 and ob > oa:
            try:
                obj = json.loads(blob[oa:ob + 1])
                arr = obj.get("tables") or obj.get("selected") or []
            except Exception:  # noqa: BLE001
                arr = None
    out: list[dict] = []
    for item in arr or []:
        if isinstance(item, dict):
            name = item.get("table_name") or item.get("name") or ""
            if name:
                out.append({"id": item.get("id"), "table_name": name,
                            "comment": item.get("comment") or ""})
        elif isinstance(item, str) and item.strip():
            out.append({"id": None, "table_name": item.strip(), "comment": ""})
    return out


_MEASURE_KW = None  # 占位移除标记（检索权重已参考实现简化，不再用规则加权）


def _search_tables(db, datasource_id: int, schema_name: str | None,
                   table_hints: list[str]) -> tuple[list[TableMeta], dict[int, list[tuple[str, str]]]]:
    """四维度表检索（参考 UnifiedQA _fetch_matching_tables）：
    对每个检索词按「表名 / 表注释 / 字段注释」三维度 LIKE 匹配（本系统无 usage_guide
    使用说明表与 table_desc 表描述字段，表描述并入表注释维度），UNION 去重，
    排序：字段注释命中的表（用户要查的量直接落在字段上，如 file_size 文件大小（字节））
    → BASE 表优先 → 表名。
    返回 (候选表列表, 字段命中证据 {table_id: [(column, comment), ...]})。"""
    if not table_hints:
        return [], {}
    all_tables = _all_business_tables(db, datasource_id, schema_name)
    if not all_tables:
        return [], {}
    by_id = {t.id: t for t in all_tables}
    table_ids = [t.id for t in all_tables]
    cols = (db.query(ColumnMeta)
            .filter(ColumnMeta.table_meta_id.in_(table_ids))
            .order_by(ColumnMeta.ordinal).all())
    col_by_table: dict[int, list[ColumnMeta]] = {}
    for c in cols:
        col_by_table.setdefault(c.table_meta_id, []).append(c)

    # Step1：四维度 LIKE 匹配（表名/表注释/字段注释），按表去重（参考实现 UNION + seen 去重）
    hit_ids: set[int] = set()
    column_hit_ids: set[int] = set()  # 字段注释维度命中的表（最直接的表信号）
    evidence: dict[int, list[tuple[str, str]]] = {}
    name_hit_ids: set[int] = set()  # 表名/表注释维度命中（权重最高）
    for h in table_hints:
        if not h:
            continue
        hl = h.lower()
        for t in all_tables:
            if t.id in hit_ids:
                continue
            tname = (t.table_name or "").lower()
            if hl and hl in tname:
                hit_ids.add(t.id)
                name_hit_ids.add(t.id)
                continue
            blob = f"{t.table_name} {t.comment or ''}".lower()
            if hl and hl in blob:
                hit_ids.add(t.id)
                name_hit_ids.add(t.id)
        # 字段注释维度（参考实现维度4）：独立收集命中与证据，不受表名/注释先命中影响；
        # 用户确认：字段命中权重低于表名/注释命中——排序靠后，但表不丢（保留在候选池）
        for c in cols:
            cblob = f"{c.column_name} {c.comment or ''}".lower()
            if hl and hl in cblob:
                hit_ids.add(c.table_meta_id)
                column_hit_ids.add(c.table_meta_id)
                evidence.setdefault(c.table_meta_id, []).append((c.column_name, c.comment or ""))

    # Step2：排序——表名/表注释命中（权重最高，用户确认）→ BASE 表优先 → 表名；
    # 字段命中表不丢（保留在候选池），由选表阶段【优先候选区】按量词字段再呈现
    def _sort_key(tid: int) -> tuple:
        t = by_id[tid]
        return (0 if tid in name_hit_ids else 1,
                0 if (t.table_type or "").upper().startswith("BASE") else 1,
                (t.table_name or "").lower())

    hits = [by_id[tid] for tid in sorted(hit_ids, key=_sort_key)]
    return hits, evidence


def _table_hint_hits(db, datasource_id: int, schema_name: str | None,
                     table_hints: list[str]) -> list[TableMeta]:
    """四维检索的表候选（兼容旧调用）。"""
    hits, _ = _search_tables(db, datasource_id, schema_name, table_hints)
    return hits


def _llm_select_tables(datasource_id: int, question: str,
                       schema_name: str | None = None,
                       llm: LLMClient | None = None,
                       history: list[dict] | None = None,
                       table_hints: list[str] | None = None,
                       target_fields: list[str] | None = None
                       ) -> tuple[list[TableMeta], dict]:
    """V1.2 两阶段选表（Step2）。
    1) 全量业务表（无 LIMIT）清单（id+表名+注释）拼入 user prompt；
    2) 多轮澄清已确认表（「已确认查询表：X、Y」）→ 直接锁定，不再 LLM 选表；
    3) 问题拆表检索词（table_hints，问题重构提取）预筛全量表：
       - 命中 ≤30 张 → **确定性直接采用命中表（跳过 LLM）**，不再全表澄清；
       - 命中 >30 张 → 选表清单收窄为命中表（前 60 张），LLM 只需从中挑选；
       - 无命中 → 维持全量清单；
    4) LLM 输出 [{id,table_name,comment}]，按 id/表名匹配，全量采纳不截断；
    5) LLM 未配置→返回空（上层走 mock）；解析失败重试 1 次，仍失败抛 TableSelectError。
    返回 (选中表列表, 选表元信息 {source, raw, total})。"""
    db = SessionLocal()
    try:
        all_tables = _all_business_tables(db, datasource_id, schema_name)
        hint_hits, hint_evidence = _search_tables(
            db, datasource_id, schema_name, table_hints or [])
    finally:
        db.close()
    meta = {"source": "none", "raw": [], "total": len(all_tables),
            "hint_hits": [t.table_name for t in hint_hits],
            "hint_evidence": {str(tid): cols for tid, cols in hint_evidence.items()}}
    if not all_tables:
        return [], meta
    if hint_hits:
        logger.info("[问数][选表] 四维检索词 %s 命中 %d 张（收窄选表范围）: %s",
                    table_hints, len(hint_hits),
                    [t.table_name for t in hint_hits][:15])
    if hint_evidence:
        by_id = {t.id: t for t in all_tables}
        _ev = {by_id[tid].table_name: [c for c, _ in cols[:3]]
               for tid, cols in hint_evidence.items() if tid in by_id}
        logger.info("[问数][选表] 字段注释命中 %d 张（字段证据，供生成阶段引用）: %s",
                    len(_ev), _ev)

    by_name = {t.table_name: t for t in all_tables}

    # 1) 多轮已确认表：直接锁定
    confirmed = _parse_confirmed_tables(question, history)
    if confirmed:
        picked = [by_name[n] for n in confirmed if n in by_name]
        if picked:
            meta.update(source="confirmed",
                        raw=[{"id": t.id, "table_name": t.table_name, "comment": t.comment or ""}
                             for t in picked])
            logger.info("[NL2SQL] 选表(多轮已确认): %s", [t.table_name for t in picked])
            return picked, meta

    if llm is None or not getattr(llm, "configured", False):
        return [], meta  # 无 LLM：上层走 mock 演示模式

    # 2) 选表清单：拆表命中 >30 张 → 收窄为命中表（前 120 张，字段命中表已在排序最前），
    #    无命中 → 全量。参考实现无收窄（全量给 LLM），此处 120 张为 prompt 大小平衡。
    if hint_hits:
        select_pool = hint_hits[:120]
        pool_label = f"（拆表检索词命中 {len(hint_hits)} 张，已收窄为前 {len(select_pool)} 张）"
    else:
        select_pool = all_tables
        pool_label = ""
    catalog = "\n".join(
        f'- {{"id": {t.id}, "table_name": "{t.table_name}", "comment": "{(t.comment or "").replace(chr(34), " ")}"}}'
        for t in select_pool)
    # 字段命中证据注入选表 prompt（参考系统把检索文档含字段信息直接给 LLM）：
    # 命中「大小/上传/附件类量词字段」的表单独成【优先候选】区（用户要查的量的载体或
    # 业务状态锚点，如 size 文件大小 / upload_status 上传状态），无注释的表
    # （如 attachment_preview_record）靠这些字段让 LLM 识别业务含义
    _ev_lines = []
    _strong_ids: set[int] = set()
    if hint_evidence:
        _by_id = {t.id: t for t in select_pool}
        _size_cols = ("大小", "字节", "size", "长度", "体积", "上传", "附件")
        for tid, cols in hint_evidence.items():
            if tid not in _by_id:
                continue
            strong = [(c, cm) for c, cm in cols
                      if any(k in (cm or "").lower() or k in c.lower() for k in _size_cols)]
            if not strong:
                continue
            _strong_ids.add(tid)
            _ev_lines.append(
                f"- {_by_id[tid].table_name}: " + "、".join(
                    f"{c}（{cm or '无注释'}）" for c, cm in strong[:4]))
    evidence_block = (
        "\n【文件大小类字段命中（用户要查的量在这些表的字段上，优先考虑）】\n"
        + "\n".join(_ev_lines)
        if _ev_lines else "")
    # 用户查询目标（spec 解析出的指标/维度名，如"文件大小"）→ 选表时的字段维度提示
    target_block = ""
    if target_fields:
        target_block = (
            "\n【用户查询目标（请优先选包含这些目标字段的表）】"
            + "、".join(target_fields[:8]) + "\n")
    # 优先候选区：命中用户量词/状态锚点字段的表置顶并标注，其余候选跟在后面（信息分层，
    # 减少 LLM 在 100+ 张清单中决策的不稳定性）
    _strong = [t for t in select_pool if t.id in _strong_ids]
    _rest = [t for t in select_pool if t.id not in _strong_ids]
    if _strong:
        catalog = (
            "【优先候选表（字段命中用户量词/状态锚点，最可能相关）】\n"
            + "\n".join(
                f'- {{"id": {t.id}, "table_name": "{t.table_name}", "comment": "{(t.comment or "").replace(chr(34), " ")}"}}'
                for t in _strong)
            + "\n【其他候选表】\n"
            + "\n".join(
                f'- {{"id": {t.id}, "table_name": "{t.table_name}", "comment": "{(t.comment or "").replace(chr(34), " ")}"}}'
                for t in _rest))
    hist_lines = []
    for m in (history or [])[-4:]:
        role_label = "用户" if m.get("role") == "user" else "助手"
        hist_lines.append(f"{role_label}: {m.get('text', '')}")
    history_block = "\n".join(hist_lines) or "（无）"
    user_prompt = (
        f"【数据表清单（schema: {schema_name or '全部'}，共 {len(select_pool)} 张{pool_label}）】\n"
        f"{catalog}\n{target_block}\n{evidence_block}\n\n"
        f"【历史对话】\n{history_block}\n\n"
        f"【用户问题】{question}\n\n"
        "只输出 JSON 数组：[{\"id\":表ID,\"table_name\":\"表名\",\"comment\":\"表注释\"}, ...]，无其他文字。")

    picked: list[TableMeta] = []
    last_exc = None
    for attempt in range(2):  # 失败重试 1 次
        try:
            content = llm.chat(
                [{"role": "system", "content": SELECT_TABLE_SYSTEM},
                 {"role": "user", "content": user_prompt}],
                temperature=0.0, max_tokens=1200, thinking=False)
            items = _parse_select_blob(content)
            if not items:
                raise ValueError("选表结果为空数组或无法解析")
            # 按 id 优先、表名兜底匹配；保持 LLM 输出顺序、去重、不截断
            seen: set[int] = set()
            for it in items:
                t = None
                if it["id"] is not None:
                    t = next((x for x in select_pool if x.id == it["id"]), None)
                if t is None:
                    t = by_name.get(it["table_name"])
                if t and t.id not in seen:
                    seen.add(t.id)
                    picked.append(t)
            if not picked:
                raise ValueError("选中表均不在清单内")
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.info("[NL2SQL] 选表第 %d 次失败: %s | LLM原文: %r",
                        attempt + 1, exc, (content if "content" in dir() else "")[:200])
    if not picked:
        raise TableSelectError(f"无法识别本次查询相关的数据表（{last_exc}），请补充说明要查询的业务对象")
    if len(picked) > 20:
        logger.warning("[NL2SQL] 选表数量较多: %d 张（不截断，仅提示）", len(picked))
    meta.update(source="llm",
                raw=[{"id": t.id, "table_name": t.table_name, "comment": t.comment or ""}
                     for t in picked])
    logger.info("[NL2SQL] 选表(LLM): 全表%d张 → 选中 %s",
                len(all_tables), [t.table_name for t in picked])
    return picked, meta


def retrieve_few_shots(workspace_id: int, question: str, top_k: int = 3) -> list[dict]:
    """few-shot：向量检索 FAQ（含 sql_example）+ 关键词 SqlExample 兜底。"""
    from .rag import retrieve_few_shot_vectors
    db = SessionLocal()
    try:
        hits = retrieve_few_shot_vectors(workspace_id, question, top_k=top_k)
        if hits:
            out = []
            for h in hits:
                ex = h.get("sql_example") or ""
                if ex:
                    out.append({"question": h.get("question") or h["content"][:60],
                                "sql": ex})
            if out:
                return out
        # 兜底：SqlExample 关键词
        exs = (db.query(SqlExample)
               .filter(SqlExample.workspace_id == workspace_id,
                       SqlExample.enabled.is_(True)).all())
        q = question.lower()
        words = set(re.findall(r"[\u4e00-\u9fa5_a-z0-9]+", q))
        scored = []
        for e in exs:
            score = sum(2 for w in words if w and w in e.question.lower())
            if score > 0:
                scored.append((score, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"question": e.question, "sql": e.sql_text}
                for _, e in scored[:top_k]]
    finally:
        db.close()


def _detect_clarify(question: str, datasource_id: int,
                    tables: list[TableMeta] | None = None) -> list[dict] | None:
    """V1.1 Step6 澄清：检测是否存在 ≥2 张语义相近表且问题无明确限定词。
    - tables 非空：只在「LLM 选中表」范围内检测（精准，避免全表误报）；
    - tables 为空：回退全量表扫描；
    - 问题明确包含某张表名/完整注释核心（≥5 字，如「接口测试用例」）→ 不歧义；
    - 问题仅命中多张表共有的实体片段（如「测试用例」同时命中功能/接口测试用例表）→ 触发澄清。
    多轮「已确认查询表：X」消息中含表名 → 不歧义。"""
    db = SessionLocal()
    try:
        if tables:
            all_t = list(tables)
        else:
            all_t = (db.query(TableMeta)
                     .filter(TableMeta.datasource_id == datasource_id,
                             TableMeta.deprecated.is_(False))
                     .filter(~TableMeta.table_name.in_(SYSTEM_TABLE_BLACKLIST)).all())
    finally:
        db.close()
    if len(all_t) < 2:
        return None
    qq = re.sub(r"\s+", "", question.lower())
    cores: list[tuple] = []
    for t in all_t:
        core = re.sub(r"[表库]$", "", (t.comment or "").lower())
        if len(core) >= 4:
            cores.append((t, core))
    # 0) 问题已明确指定表名（含多轮澄清确认「已确认查询表：api_test_cases」）→ 不歧义
    for t in all_t:
        if t.table_name.lower() in qq:
            return None
    # 1) 完整注释核心命中（含业务修饰词）→ 明确指向单表
    for _t, core in cores:
        if len(core) >= 5 and core in qq:
            return None
    # 2) 公共实体片段（≥4 字）同时命中 ≥2 张选中表注释且在问题中出现 → 歧义
    candidates: list[tuple] = []
    for t, core in cores:
        hit: set[str] = set()
        for ln in range(min(8, len(core)), 3, -1):
            segs = {core[i:i + ln] for i in range(len(core) - ln + 1)}
            if any(s in qq for s in segs):
                hit = segs
                break
        if hit:
            candidates.append((t, hit))
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            ti, si = candidates[i]
            tj, sj = candidates[j]
            common = si & sj
            core = max(common, key=len) if common else ""
            if len(core) >= 4:
                return [{"table": ti.table_name, "comment": ti.comment or ""},
                        {"table": tj.table_name, "comment": tj.comment or ""}]
    return None


def _count_shape_error(sql: str, spec_context: dict | None) -> str | None:
    """计数形状校验：spec 含 count 指标时，SQL 必须含聚合函数（COUNT/SUM…）。
    防止 LLM 把"X数/次数"类计数问题生成成明细行查询（SELECT ... LIMIT n）。"""
    if not spec_context:
        return None
    metrics = (spec_context.get("spec") or {}).get("metrics") or []
    def _agg(m) -> str:
        if isinstance(m, dict):
            return m.get("agg") or ""
        if isinstance(m, (list, tuple)) and len(m) >= 2:
            return str(m[1])
        return ""
    if not any(_agg(m) == "count" for m in metrics):
        return None
    import sqlglot
    import sqlglot.expressions as exp
    try:
        ast = sqlglot.parse_one(sql, read="mysql")
    except Exception:  # noqa: BLE001
        return None
    if not list(ast.find_all(exp.AggFunc)):
        return ("用户询问数量/计数（如 X数/多少次/总量），SQL 必须输出聚合查询："
                "包含聚合函数 COUNT/SUM 并按分组字段 GROUP BY，禁止 SELECT 明细列 LIMIT n 的明细行查询")
    # 无维度的纯聚合总量查询不应 GROUP BY（LLM 常误套排行模板 GROUP BY 1 ORDER BY 1 DESC LIMIT 1，
    # MySQL 会报「Can't group on '别名'」）
    dims = (spec_context.get("spec") or {}).get("dimensions") or []
    if not dims and list(ast.find_all(exp.Group)):
        projections = ast.expressions or []

        def _is_agg_or_literal(e: Any) -> bool:
            if isinstance(e, exp.AggFunc):
                return True
            if isinstance(e, exp.Alias) and isinstance(e.this, exp.AggFunc):
                return True
            if isinstance(e, exp.Literal):
                return True
            return False

        if projections and all(_is_agg_or_literal(p) for p in projections):
            return ("该问题为纯总量/计数查询（无分组维度），SQL 不应包含 GROUP BY："
                    "直接 SELECT COUNT(*) / SUM(...) 即可；GROUP BY 聚合列会导致 "
                    "MySQL 报错「Can't group on '别名'」或返回错误结果。"
                    "若用户确实要按某维度分组，请在维度中明确该字段。")
    return None


def _unexpected_filter_error(sql: str, question: str) -> str | None:
    """检测 WHERE 中用户问题未提及的常见脑补过滤字段（status/is_active/type 等）。
    防止 LLM 自行添加业务含义过滤（如 status=1 仅统计已结束），用户未声明即不得过滤。
    软删字段 is_deleted 按规则 7 自动注入，在此豁免。"""
    import sqlglot
    import sqlglot.expressions as exp
    try:
        ast = sqlglot.parse_one(sql, read="mysql")
    except Exception:  # noqa: BLE001
        return None
    where = ast.find(exp.Where)
    if not where:
        return None
    col_names = set()
    for col in where.find_all(exp.Column):
        name = (col.name or "").lower()
        if name:
            col_names.add(name)
    # 软删字段豁免（规则 7 自动注入）
    col_names.discard("is_deleted")
    col_names.discard("deleted")
    # 常见脑补字段 → 用户问题中应出现的中文关键词（关键词不宜过泛，避免被常见词误豁免）
    suspect: dict[str, list[str]] = {
        "status": ["状态", "已结束", "处理中", "未通过", "撤销", "完成", "待审", "审批中", "通过", "驳回", "已办", "待办"],
        "state": ["状态", "已结束", "处理中", "完成"],
        "is_active": ["有效", "启用", "在用", "无效", "停用", "激活"],
        "is_valid": ["有效", "无效", "合法", "校验"],
        "type": ["类型", "类别"],
        "category": ["分类", "类别"],
        "level": ["级别", "等级", "优先级"],
    }
    for field, keywords in suspect.items():
        if field in col_names and not any(kw in question for kw in keywords):
            return (f"SQL 的 WHERE 中包含用户问题未提及的过滤字段 `{field}`。"
                    f"请直接删除该过滤条件，不要在 WHERE 中添加任何用户问题未提及的字段；"
                    f"若确实需要过滤，请在问题中明确说明（如「状态=已结束」）。")
    return None


def _ratio_denominator_error(sql: str, question: str,
                             spec_context: dict | None = None) -> str | None:
    """占比口径校验：占比（占比/比例/构成/百分比）的分母必须是同口径全量统计。

    禁止用 TOP N 小计做分母——典型错误形态：
    WITH daily_stats AS (… LIMIT 3) SELECT …, ROUND(x * 100.0 / (SELECT SUM(x) FROM daily_stats), 2)
    此时 daily_stats 只含前三，占比=前三占前三（合计恒 100%），与用户要的"占总量比例"无关。
    检测：除法分母是标量子查询，且该子查询自身含 LIMIT，或其 FROM 引用的 CTE 含 LIMIT。
    """
    ratio_words = ("占比", "比例", "构成", "百分比", "份额", "比重")
    if not any(w in question for w in ratio_words):
        if spec_context:
            act = (spec_context.get("spec") or {}).get("action") or {}
            if str(act.get("stat") or "") != "ratio":
                return None
        else:
            return None
    import sqlglot
    import sqlglot.expressions as exp
    try:
        ast = sqlglot.parse_one(sql, read="mysql")
    except Exception:  # noqa: BLE001
        return None
    # 含 LIMIT 的 CTE 名集合（TOP N 小计）
    limited_ctes: set[str] = set()
    for cte in ast.find_all(exp.CTE):
        body = cte.this if isinstance(cte.this, exp.Query) else None
        if body is not None and list(body.find_all(exp.Limit)):
            try:
                limited_ctes.add(cte.alias_or_name)
            except Exception:  # noqa: BLE001
                pass

    def _denom_is_limited(right: exp.Expression) -> bool:
        subs = [right] if isinstance(right, exp.Subquery) else list(right.find_all(exp.Subquery))
        for sq in subs:
            if list(sq.find_all(exp.Limit)):
                return True
            for tbl in sq.find_all(exp.Table):
                if tbl.name in limited_ctes:
                    return True
        return False

    for div in ast.find_all(exp.Div):
        right = getattr(div, "expression", None)
        if right is None:
            continue
        if _denom_is_limited(right):
            return ("占比（占比/比例/构成/百分比）的分母必须是同口径全量统计："
                    "禁止把 TOP N 小计（含 LIMIT 的子查询/CTE）作为分母——"
                    "这会导致前三的占比合计恒为 100%，而不是占全部记录的真正比例。"
                    "请改为对全部记录的全量聚合作为分母"
                    "（如 SUM(COUNT(*)) OVER () 全量窗口，或对不含 LIMIT 的全量结果求和）。")
    return None


def _clean_alias(comment: str) -> str:
    """别名清洗：只取字段主名，去掉注释中的枚举/取值说明。
    例：「项目ID（NULL=全局，预留项目级）」→「项目ID」；「状态：pending-等待」→「状态」。"""
    for sep in ("（", "(", "：", ":", "·", "-", " "):
        idx = comment.find(sep)
        if idx > 0:
            comment = comment[:idx]
    return comment.strip()


def localize_result_columns(sql: str, columns: list[str], datasource_id: int) -> list[str]:
    """执行后兜底：把结果列头中的英文字段名替换为元数据中文注释（模板/LLM 路径双保险）。
    - 已是中文/含中文字符的列头不动；
    - 英文列头按 SQL 投影顺序解析到底层表.字段，查 column_meta.comment，经 _clean_alias 后替换；
    - 查不到注释则保留原列头。聚合/表达式列若输出名为英文且无法映射到底层字段，保留。"""
    if not columns or not sql:
        return columns
    try:
        import sqlglot
        import sqlglot.expressions as exp
        from ..database import SessionLocal
        from ..models import ColumnMeta, TableMeta
        ast = sqlglot.parse_one(sql, read="mysql")
    except Exception:  # noqa: BLE001
        return columns
    if not isinstance(ast, exp.Select):
        return columns
    # 表别名 → 真实表名
    alias_map: dict[str, str] = {}
    ref_tables: set[str] = set()
    for node in ast.find_all(exp.Table):
        if node.name:
            ref_tables.add(node.name)
            if node.alias:
                alias_map[node.alias] = node.name
    # 按投影顺序取每列的 (输出名, 底层列名, 底层表名)
    proj_info: list[tuple[str, str, str]] = []
    for proj in ast.expressions:
        out_name = proj.alias_or_name if hasattr(proj, "alias_or_name") else ""
        col_name = ""
        tbl_name = ""
        if isinstance(proj, exp.Alias):
            inner = proj.this
            out_name = proj.alias or ""
            if isinstance(inner, exp.Column):
                col_name = inner.name or ""
                tbl_name = inner.table or ""
        elif isinstance(proj, exp.Column):
            col_name = proj.name or ""
            tbl_name = proj.table or ""
            out_name = out_name or col_name
        proj_info.append((out_name or "", col_name, tbl_name))
    if len(proj_info) != len(columns):
        # 投影数与结果列数不一致（如 SELECT *），不做替换
        return columns
    # 加载相关表字段注释
    db = SessionLocal()
    try:
        tmap = {t.table_name: t for t in db.query(TableMeta)
                .filter(TableMeta.datasource_id == datasource_id,
                        TableMeta.table_name.in_(ref_tables)).all()}
        comment_map: dict[tuple[str, str], str] = {}
        for tname, tm in tmap.items():
            for c in db.query(ColumnMeta).filter(ColumnMeta.table_meta_id == tm.id).all():
                comment_map[(tname, c.column_name)] = c.comment or ""
    finally:
        db.close()

    def _is_cn(s: str) -> bool:
        return any("\u4e00" <= ch <= "\u9fff" for ch in s)

    result: list[str] = []
    for out_name, col_name, tbl_alias in proj_info:
        cur = out_name or col_name
        if _is_cn(cur) or not cur:
            result.append(cur)
            continue
        real_tbl = alias_map.get(tbl_alias, tbl_alias) if tbl_alias else ""
        cmt = ""
        if real_tbl and col_name:
            cmt = comment_map.get((real_tbl, col_name), "")
        if not cmt and col_name:
            # 表别名未解析到时，退化为按列名在所有引用表中找唯一注释
            cands = [v for (tn, cn), v in comment_map.items() if cn == col_name and v]
            if len(cands) == 1:
                cmt = cands[0]
        if cmt:
            result.append(_clean_alias(cmt))
        else:
            result.append(cur)
    return result


def _apply_column_aliases(sql: str, datasource_id: int) -> str:
    """为 SELECT 投影列自动追加中文注释别名（如 name AS `用例名称`）。
    - 已有别名的列跳过；
    - 单表字段用字段注释作别名；
    - 多表同字段（同名歧义）用 `表名-字段名` 作别名；
    - 函数/聚合列、SELECT *、子查询列不处理。"""
    import sqlglot
    import sqlglot.expressions as exp
    from ..database import SessionLocal
    from ..models import ColumnMeta, TableMeta

    try:
        ast = sqlglot.parse_one(sql, read="mysql")
    except Exception:  # noqa: BLE001
        return sql
    if not isinstance(ast, exp.Select):
        return sql

    alias_map: dict[str, str] = {}
    ref_tables: set[str] = set()
    for node in ast.find_all(exp.Table):
        if node.name:
            ref_tables.add(node.name)
            if node.alias:
                alias_map[node.alias] = node.name
    if not ref_tables:
        return sql

    db = SessionLocal()
    try:
        tmap = {t.table_name: t for t in db.query(TableMeta)
                .filter(TableMeta.datasource_id == datasource_id,
                        TableMeta.table_name.in_(ref_tables)).all()}
        col_comment: dict[tuple[str, str], str] = {}
        for tname, tm in tmap.items():
            for c in db.query(ColumnMeta).filter(ColumnMeta.table_meta_id == tm.id).all():
                col_comment[(tname, c.column_name)] = c.comment or ""
    finally:
        db.close()

    # 统计每个列名在多少张引用表中存在（用于多表同字段判断）
    col_table_count: dict[str, int] = {}
    for (tname, cname) in col_comment:
        col_table_count[cname] = col_table_count.get(cname, 0) + 1

    modified = False
    for proj in list(ast.expressions):
        if isinstance(proj, exp.Alias) or isinstance(proj, exp.Star):
            continue
        if not isinstance(proj, exp.Column):
            continue  # 函数/聚合等不处理
        cname = proj.name or ""
        if not cname:
            continue
        tbl = proj.table or ""
        real_tbl = alias_map.get(tbl, tbl) if tbl else ""
        is_ambiguous = col_table_count.get(cname, 0) > 1
        if is_ambiguous:
            # 多表同字段：用 表名-字段名 作别名
            alias_label = f"{real_tbl or cname}-{cname}"
        else:
            if real_tbl:
                alias_label = col_comment.get((real_tbl, cname), "")
            else:
                tables_with_col = [t for (t, c) in col_comment if c == cname]
                alias_label = (col_comment.get((tables_with_col[0], cname), "")
                               if len(tables_with_col) == 1 else "")
        if alias_label:
            proj.replace(proj.as_(_clean_alias(alias_label), quoted=True))
            modified = True

    if not modified:
        return sql
    return ast.sql(dialect="mysql", pretty=False)


# 全角标点 → 半角映射（LLM 输出常混入全角逗号/括号导致 SQL 语法错误）
_FULLWIDTH_MAP = {
    "，": ",", "；": ";", "：": ":", "（": "(", "）": ")",
    "“": '"', "”": '"', "‘": "'", "’": "'", "、": ",", "。": ".",
    "！": "!", "？": "?", "　": " ",
}


def _normalize_sql_punctuation(sql: str) -> str:
    """将 SQL 中的全角标点归一为半角（如别名后误用全角逗号会导致解析失败）。

    跳过单引号字符串字面量内部，避免改写 LIKE 等字面量中的真实内容。
    """
    out: list[str] = []
    in_str = False
    for ch in sql:
        if ch == "'":
            in_str = not in_str
            out.append(ch)
            continue
        out.append(ch if in_str else _FULLWIDTH_MAP.get(ch, ch))
    return "".join(out)


def _auto_strip_invalid_filters(sql: str, fields: list[str]) -> str:
    """确定性移除 WHERE/HAVING 中引用指定字段的条件（修复 LLM 幻觉过滤，无需 LLM 重试）。

    处理三种位置：中间条件（AND field op val）、首条件后还有其他（WHERE field op val AND）、
    唯一条件（WHERE field op val → WHERE 1=1）。字段可带表别名（t.field）。"""
    import re
    result = sql
    for field in fields:
        f = re.escape(field)
        val = r"(?:'[^']*'|\"[^\"]*\"|\d+(?:\.\d+)?|NULL|TRUE|FALSE)"
        op = (r"(?:(?:=|!=|<>|>=|<=|>|<)\s*" + val +
              r"|IS\s+(?:NOT\s+)?NULL"
              r"|(?:NOT\s+)?IN\s*\([^)]*\)"
              r"|(?:NOT\s+)?LIKE\s*" + val + r")")
        # 中间条件: AND t.field op value
        result = re.sub(r'\bAND\s+(?:\w+\.)?' + f + r'\s*' + op + r'\b',
                        '', result, flags=re.IGNORECASE)
        # 首条件后还有: WHERE t.field op value AND → WHERE
        result = re.sub(r'\bWHERE\s+(?:\w+\.)?' + f + r'\s*' + op + r'\s+AND\b',
                        'WHERE', result, flags=re.IGNORECASE)
        # HAVING 首条件同理
        result = re.sub(r'\bHAVING\s+(?:\w+\.)?' + f + r'\s*' + op + r'\s+AND\b',
                        'HAVING', result, flags=re.IGNORECASE)
        # 唯一条件: WHERE t.field op value (后面是 GROUP/ORDER/LIMIT/UNION/;/)/$)
        result = re.sub(r'\bWHERE\s+(?:\w+\.)?' + f + r'\s*' + op +
                        r'(?=\s*(?:GROUP|ORDER|LIMIT|UNION|;|\)|$))',
                        'WHERE 1=1', result, flags=re.IGNORECASE)
        result = re.sub(r'\bHAVING\s+(?:\w+\.)?' + f + r'\s*' + op +
                        r'(?=\s*(?:GROUP|ORDER|LIMIT|UNION|;|\)|$))',
                        'HAVING 1=1', result, flags=re.IGNORECASE)
    # 清理 WHERE 1=1 AND → WHERE
    result = re.sub(r'\bWHERE\s+1=1\s+AND\b', 'WHERE', result, flags=re.IGNORECASE)
    result = re.sub(r'\bHAVING\s+1=1\s+AND\b', 'HAVING', result, flags=re.IGNORECASE)
    return result


def _extract_bad_fields(error_msg: str) -> list[str]:
    """从校验错误信息中提取问题字段名（字段不存在 / 未提及过滤字段）。"""
    import re
    fields: list[str] = []
    # "字段不存在：is_deleted, record_flow.status" 或 "字段不存在：is_deleted；..."
    m = re.search(r'字段不存在[：:]\s*(.+?)(?:；|$)', error_msg)
    if m:
        for part in re.split(r'[,，]', m.group(1)):
            col = part.strip().split('.')[-1].strip('`\'\" ')
            if col and '（' not in col and '可用列' not in col:
                fields.append(col)
    # "未提及的过滤字段 `status`"
    m = re.search(r'未提及的过滤字段\s*[`\'"]?(\w+)[`\'"]?', error_msg)
    if m:
        fields.append(m.group(1))
    return list(dict.fromkeys(fields))  # 去重保序


def _auto_qualify_ambiguous_columns(sql: str, datasource_id: int) -> str:
    """确定性自动修复：JOIN 多表时，WHERE/GROUP BY/ORDER BY/HAVING 中未限定的歧义列
    用 FROM 中第一个表限定。仅修改未限定且确实在多表中存在的列。"""
    import sqlglot
    import sqlglot.expressions as exp
    from ..executor import get_table_columns

    try:
        ast = sqlglot.parse_one(sql, read="mysql")
    except Exception:  # noqa: BLE001
        return sql

    changed = False
    for sel in ast.find_all(exp.Select):
        sel_tables = [t.name for t in sel.find_all(exp.Table)]
        if len(sel_tables) < 2:
            continue
        first_table = sel_tables[0]
        # 获取列集合
        table_cols: dict[str, set[str]] = {}
        for tname in sel_tables:
            try:
                cols = get_table_columns(datasource_id, tname)
                table_cols[tname] = {c["column_name"] for c in cols} if isinstance(cols, list) else set()
            except Exception:  # noqa: BLE001
                table_cols[tname] = set()
        # 检查 WHERE/GROUP BY/ORDER BY/HAVING 中的未限定列
        for key in ("where", "group", "order", "having"):
            clause = sel.args.get(key)
            if not clause:
                continue
            for col in clause.find_all(exp.Column):
                if col.table:
                    continue
                cname = col.name or ""
                if not cname:
                    continue
                present_in = [t for t in sel_tables if cname in table_cols.get(t, set())]
                if len(present_in) >= 2:
                    col.set("table", exp.to_identifier(first_table))
                    changed = True
    if changed:
        return ast.sql(dialect="mysql")
    return sql


def _run_validations(sql: str, datasource_id: int, question: str, dialect: str) -> str:
    """运行全部 SQL 校验链，通过后应用中文字段别名并返回最终 SQL；失败抛异常。"""
    from ..executor import validate_sql
    validate_sql(sql, dialect)
    _check_tables_exist(sql, datasource_id)
    _check_columns_exist(sql, datasource_id)
    _check_cte_alias_scope(sql)
    _check_ambiguous_columns(sql, datasource_id)
    _check_id_display(sql, datasource_id)
    shape_err = _count_shape_error(sql, None)
    if shape_err:
        raise ValueError(shape_err)
    filter_err = _unexpected_filter_error(sql, question)
    if filter_err:
        raise ValueError(filter_err)
    ratio_err = _ratio_denominator_error(sql, question, None)
    if ratio_err:
        raise ValueError(ratio_err)
    return _apply_column_aliases(sql, datasource_id)


def generate_sql(datasource_id: int, workspace_id: int, question: str,
                 history: list[dict] | None = None,
                 llm: LLMClient | None = None,
                 retries: int | None = None,
                 exec_error: str | None = None) -> dict:
    """生成 SQL；返回 {"intent","sql","explain","tables"}。
    校验失败自动回灌重试（≤ retries 次）。llm 为 None 时走 mock 演示模式。"""
    db = SessionLocal()
    rag_faq: list[dict] = []
    rag_doc: list[dict] = []
    try:
        ds = db.query(Datasource).get(datasource_id)
        if not ds:
            raise LLMError("数据源不存在")
        dialect = ds.type
        _hints: list[str] = []
        _target: list[str] = []
        try:
            tables, select_meta = _llm_select_tables(
                datasource_id, question, llm=llm, history=history,
                table_hints=_hints, target_fields=_target)
        except TableSelectError as exc:
            # 回退为表级澄清：候选优先 = 拆表检索词命中表（收窄），无命中才全表
            logger.warning("[问数][选表] LLM 选表失败(同步)，回退表候选澄清: %s", exc)
            cand = []
            try:
                db2 = SessionLocal()
                try:
                    hits = _table_hint_hits(db2, datasource_id, None, _hints)
                    if hits:
                        cand = [{"table": t.table_name, "comment": t.comment or ""}
                                for t in hits[:60]]
                    else:
                        all_t = _all_business_tables(db2, datasource_id, None)
                        cand = [{"table": t.table_name, "comment": t.comment or ""}
                                for t in all_t]
                finally:
                    db2.close()
            except Exception as e2:  # noqa: BLE001
                logger.warning("[问数][选表] 读取全表候选失败: %s", e2)
            if cand and _hints:
                cand.sort(key=lambda c: _hint_hit_index(c["table"], c.get("comment") or "", _hints))
            # 关系图收窄候选表：过滤噪声表 + 事实表置顶 + top 8
            try:
                from .id_resolver import narrow_candidate_tables
                cand = narrow_candidate_tables(datasource_id, cand, question=question)
            except Exception:  # noqa: BLE001
                pass
            return {"intent": "clarify", "sql": "",
                "explain": f"未能自动识别与问题相关的数据表（{exc}）。请在下方勾选需要查询的表（可多选，将分别查询）：",
                "tables": [c["table"] for c in cand],
                "candidates": cand, "original_question": question,
                "table_hints": _hints}
        select_meta["raw"] = [{"id": t.id, "table_name": t.table_name, "comment": t.comment or ""}
                              for t in tables]
        # 歧义检测（V1.1：基于 LLM 选中表，相近表≥2 且问题无明确限定词 → 先澄清）
        # 用户已点选确认表（source=confirmed）时不再重复澄清
        is_confirmed = select_meta.get("source") == "confirmed"
        clarify = None if is_confirmed else _detect_clarify(question, datasource_id, tables)
        if clarify:
            cand_text = "；".join(f"{c['table']}（{c['comment'] or '无注释'}）" for c in clarify)
            return {"intent": "clarify", "sql": "",
                    "explain": f"您的问题对应多张表，请勾选需要查询的表（可多选，将分别查询）：{cand_text}",
                    "tables": [c["table"] for c in clarify],
                    "candidates": clarify,
                    "original_question": question}
        # spec 已映射字段（mapping）与检索命中字段（hint_evidence）→ 优先推荐 + schema 分层
        _preferred: set[str] = set()
        _recommend_lines: list[str] = []
        _mapping: dict = {}
        for _m in (_mapping.get("metrics") or []) + (_mapping.get("dimensions") or []):
            col = _m.get("column") or ""
            cmt = _m.get("comment") or ""
            if col:
                _preferred.add(col)
                _recommend_lines.append(
                    f"- {_m.get('name') or col} → {col}（{cmt or '无注释'}）")
        if _recommend_lines:
            logger.info("[问数][选表] spec 映射字段注入优先推荐: %s",
                        [l.split(' → ')[0] for l in _recommend_lines])
        # 检索命中字段证据（四维检索：字段注释命中）
        _evidence = select_meta.get("hint_evidence") or {}
        _by_tid = {t.id: t.table_name for t in tables}
        _evidence_lines: list[str] = []
        if _evidence:
            for _tid, _cols in _evidence.items():
                if _tid not in _by_tid:
                    continue
                for _c, _cm in _cols:
                    if _c:
                        _preferred.add(_c)
                _evidence_lines.append(
                    f"- {_by_tid[_tid]}: " + "、".join(
                        f"{c}（{cm or '无注释'}）" for c, cm in _cols[:4]))
        schema = (_schema_text(datasource_id, tables, preferred=_preferred)
                  if tables else "（未匹配到相关数据表，说明当前问题不属于数据查询范畴）")
        joins = _join_paths_text(datasource_id, tables)
        evidence_section = (
            "\n【检索证据（拆表词/字段注释命中依据，供参考选表理由）】\n"
            + "\n".join(_evidence_lines)
            if _evidence_lines else "")
        recommend_section = (
            "\n【优先推荐字段（用户问题解析出的目标字段，★字段建议优先使用，可结合语义微调）】\n"
            + "\n".join(_recommend_lines)
            if _recommend_lines else "")
        # 用户已确认表（多轮澄清点选）：强制大模型综合考虑所有确认表，不得遗漏
        confirmed_section = ""
        if select_meta.get("source") == "confirmed" and len(tables) >= 2:
            confirmed_names = "、".join(t.table_name for t in tables)
            confirmed_section = f"""
【用户已确认选择的表（多轮澄清中明确勾选，共{len(tables)}张）】
{confirmed_names}
生成 SQL 时必须综合考虑以上所有确认表，不得遗漏任何一张（除非该表与问题完全无关，须在注释中说明原因）。

【场景一：各确认表统计同一指标的不同业务类型（如不同类型的用例/订单/记录）——最常用】
必须严格按以下模板生成（禁止用一个统计CTE去LEFT JOIN另一个统计CTE，会导致独有维度丢失）：
WITH stat_a AS (
    SELECT 表A.维度外键 AS dim_key, COUNT(*) AS cnt_a
    FROM 表A WHERE 表A.is_deleted = 0 GROUP BY 表A.维度外键
),
stat_b AS (
    SELECT 表B.维度外键 AS dim_key, COUNT(*) AS cnt_b
    FROM 表B WHERE 表B.is_deleted = 0 GROUP BY 表B.维度外键
)
SELECT
    dim.name AS 维度名称,
    COALESCE(stat_a.cnt_a, 0) AS 类型A数量,
    COALESCE(stat_b.cnt_b, 0) AS 类型B数量
FROM 维度主表 dim
LEFT JOIN stat_a ON stat_a.dim_key = dim.id
LEFT JOIN stat_b ON stat_b.dim_key = dim.id
WHERE dim.is_deleted = 0
ORDER BY 类型A数量 DESC, 类型B数量 DESC;
关键规则：
- CTE 必须按「维度外键」（如 project_id）聚合，禁止按名称聚合；
- 外层必须以「维度主表」（如 test_projects）为 FROM 主体，LEFT JOIN 各统计 CTE；
- 关联条件用 id（dim.id = stat.dim_key），禁止用名称关联；
- 禁止 FULL OUTER JOIN（MySQL 不支持）；禁止 stat_a LEFT JOIN stat_b（会丢失 stat_b 独有的维度）。

【场景二：表间存在外键关联且需联合过滤】
使用标准 JOIN...ON 关联查询，关联条件严格遵循主键-外键。

【场景三：UNION ALL】
仅在各 SELECT 列数、列类型完全一致时使用，外层只引用第一个 SELECT 的列别名；列数不一致时禁止使用（会报 1222 错误）。
"""
        examples = retrieve_few_shots(workspace_id, question)
        example_text = "\n".join(
            f"问题：{e['question']}\nSQL：{e['sql']}" for e in examples) or "（暂无示例）"
        # 知识库文档 RAG 上下文
        from .rag import retrieve_doc_context
        doc_context = retrieve_doc_context(workspace_id, question)
    finally:
        db.close()

    history_text = ""
    if history:
        lines = []
        for m in history[-6:]:
            role_label = "用户" if m["role"] == "user" else "助手"
            line = f"{role_label}: {m.get('text', '')}"
            # 多轮追问：带上一次生成的 SQL，让 LLM 基于上一次结果改写（如"再按月份拆分""只看TOP10"）
            prev_sql = m.get("sql") or ""
            if prev_sql:
                line += f"\n  [上一次SQL] {prev_sql}"
            lines.append(line)
        history_text = "\n".join(lines)

    # 先 RAG 检索（FAQ + 文档），命中内容注入意图识别（未命中/不一致则绕过 RAG 增强）
    from .rag import hybrid_search
    rag_faq = hybrid_search(workspace_id, question, kind="faq", top_k=5, min_score=0.65)
    rag_doc = hybrid_search(workspace_id, question, kind="doc", top_k=5, min_score=0.5)
    _rag_hits = {"faq": rag_faq, "doc": rag_doc}
    rag_faq_text = "\n".join(
        f"- Q：{h['content'].split(chr(10))[0][3:]}\n  A：{chr(10).join(h['content'].split(chr(10))[1:]).lstrip('A：')}"
        if h.get('content') else ""
        for h in rag_faq) or "（无命中）"
    rag_doc_text = "\n".join(f"- {h['content'][:400]}" for h in rag_doc) or "（无命中）"

    user_prompt = f"""【历史对话】
{history_text or "（无）"}
{confirmed_section}
【可用表与字段（格式：表名: 字段(类型·注释)，★=优先推荐字段，已按选中表过滤）】
{schema}
{recommend_section}
{evidence_section}

【JOIN 路径】
{joins}

【相似示例】
{example_text}

【知识库 FAQ 命中】（与用户问题一致或高度相似才列出）
{rag_faq_text}

【知识库文档命中】（可直接回答用户问题才列出）
{rag_doc_text}

【判定规则】
- 用户问题与【知识库 FAQ 命中】一致/高度相似，或【知识库文档命中】可直接回答 → intent=knowledge（sql 置空）；
- 否则需要查询数据库数据（【可用表与字段】有相关表）→ intent=query，给出 sql；
- 否则通用对话/创作/翻译/写作/编程等不依赖数据库的任务 → intent=chat（explain 放完整回答）；
- 否则恶意请求/违法内容/完全无意义 → intent=refuse。
- 判断为 query 时，直接输出一个完整闭合的 ```sql 代码块（SELECT），不要输出 intent 标记；
- 判断为 chat/knowledge/refuse 时，第一行必须独占输出 `intent=chat` / `intent=knowledge` / `intent=refuse` 标记，第二行起为自然语言回答，**禁止**为了凑 SQL 而编造不存在的表或字段。

【用户问题】{question}

数据查询严格按系统提示词「四、输出格式」仅输出一个完整闭合的 ```sql 代码块（含必要注释），无多余文字；非数据查询按「五、判定规则」首行输出 intent 标记后再写回答。"""
    if exec_error:
        user_prompt += f"""
【上次执行失败，必须修正】
{exec_error}
请分析错误原因并修正 SQL：**保持用户问题的查询语义不变**（项目/时间/状态等过滤条件、分组维度、计数/聚合形态都不得删减或改变），只修正报错本身；只使用【可用表与字段】中确切存在的表名和字段；聚合查询中 GROUP BY 必须包含 SELECT 中全部非聚合列（注意 only_full_group_by 模式）；若错误为 Subquery returns more than 1 row，必须把 `= (SELECT ...)` 改为 `IN (SELECT ...)`；若错误为 Column 'xxx' in field list is ambiguous（列名歧义），必须为 SELECT、ORDER BY、WHERE、GROUP BY 中的重名列显式加上表别名限定（如 stat_a.xxx、stat_b.xxx），并保证 JOIN 条件与 SELECT 列使用同一别名；若错误为 CTE 作用域错误（某字段已在 CTE 中被别名化），必须将后续查询中对该原始列名的引用全部替换为 CTE 输出的别名；若错误为字段不存在（尤其是 is_deleted 等软删字段），必须直接删除 SQL 中所有对该字段的引用（如 WHERE 条件），不要尝试用其他字段替代；若错误为 Not unique table/alias（表别名重复），必须为 FROM/JOIN 中的每个表分配唯一别名（如 t1、t2、sub_a、sub_b），并同步更新 SELECT/WHERE/GROUP BY/ORDER BY/JOIN ON 中对该别名的所有列引用；修正后**仅输出**修正后的 ```sql 代码块（含必要注释）。"""

    client = llm
    if client is None or not client.configured:
        yield {"type": "result", "result": _mock_sql(datasource_id, question, dialect)}
        return

    from .sql_extractor import extract_first_sql
    from ..executor import validate_sql
    messages = [{"role": "system", "content": _load_sql_system_prompt()},
                {"role": "user", "content": user_prompt}]
    from ..config import get_settings
    attempts = (retries if retries is not None else get_settings().sql_correct_retries)
    last_error = ""

    for i in range(attempts + 1):
        # 流式调用 LLM（自然语言 + markdown SQL；关闭内部 thinking 避免 Qwen3.5-4B 思考链挤占 content）
        content = ""
        stream_gen = client.chat_stream(messages, json_mode=False, max_tokens=4096, thinking=False)
        while True:
            try:
                chunk = next(stream_gen)
                # 兼容多种 chunk 载荷：dict（content/reasoning）或纯文本 str（部分 SDK 直出 delta）
                if isinstance(chunk, dict):
                    c_delta = chunk.get("content", "")
                    r_delta = chunk.get("reasoning", "")
                elif isinstance(chunk, str):
                    c_delta, r_delta = chunk, ""
                else:
                    c_delta, r_delta = str(chunk), ""
                content += c_delta
                if r_delta:
                    yield {"type": "thinking", "delta": r_delta}
                if c_delta:
                    yield {"type": "stream", "delta": c_delta}
                    # 提前中断1：检测到完整的 ```sql ... ``` 代码块就停止
                    if "```sql" in content and content.rfind("```") > content.find("```sql") + 5:
                        break
            except StopIteration as e:
                val = e.value or {}
                content = (val.get("content") or content).strip()
                break
        # 从回答中提取 SQL（markdown ```sql 代码块优先，兜底裸 SELECT）
        sql = _normalize_sql_punctuation(extract_first_sql(content) or "")
        # 排查辅助：每次 LLM 响应全文打印到日志（含提取结果）
        logger.info("[NL2SQL] 第 %d 次尝试, 提取=%s\n-----响应原文-----\n%s\n-----END-----",
                    i + 1, "OK" if sql else "未提取到SQL", content)
        if not sql:
            non_sql = _detect_non_sql_answer(content)
            if non_sql:
                # 模型判定非数据查询（chat/knowledge/refuse）：直接返回文字回答，不重试
                logger.info("[NL2SQL] 判定为 %s 回答，跳过 SQL 重试", non_sql)
                yield {"type": "result", "result": {
                    "intent": non_sql, "sql": "", "explain": _strip_intent_marker(content),
                    "tables": [t.table_name for t in tables],
                    "selected_tables": select_meta.get("raw", []),
                    "select_source": select_meta.get("source", ""),
                    "rag_hits": _rag_hits}}
                return
            last_error = "未从回答中提取到 SELECT SQL 代码块"
            # 带诊断的重试：指出上一轮具体问题，让重试有增量信息而非空转
            if "```" in content and "```sql" not in content:
                _diag = "上一次回答用了代码块但语言标记不是 sql（必须以 ```sql 开头）"
            elif "select" not in content.lower():
                _diag = "上一次回答是自然语言、完全没有 SELECT 语句；若确实无表/字段可用，按协议首行输出 intent=refuse，否则必须给出 SQL"
            else:
                _diag = "上一次回答中的 SQL 未被完整识别：必须以 ```sql 开头、以 ``` 闭合，代码块内是完整可执行的 SELECT"
            yield {"type": "retry", "msg": "未识别到 SQL，正在重试…"}
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user",
                             "content": f"【重要】{_diag}。请严格按系统提示词「四、输出格式」：**仅输出** 一个完整闭合的 ```sql 代码块（含必要注释），无任何多余文字，不要重复上一次的错误。"})
            continue

        # 校验 SQL（语法/只读/表存在/字段存在/计数形状），通过则应用中文字段别名
        logger.info("[NL2SQL] 提取后SQL前300字: %s", sql[:300])
        try:
            sql = _run_validations(sql, datasource_id, question, dialect)
            yield {"type": "result", "result": {
                "intent": "query", "sql": sql,
                "explain": content, "tables": [t.table_name for t in tables],
                "selected_tables": select_meta.get("raw", []),
                "select_source": select_meta.get("source", ""),
                "rag_hits": _rag_hits}}
            return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            logger.warning("[NL2SQL] 校验失败原因: %s | 异常类型: %s", last_error[:200], type(exc).__name__)
            # 确定性自动修复（链式）：依次尝试多种修复策略，全部失败才回退 LLM 重试
            auto_fixed = sql
            fixed_reasons: list[str] = []
            for _attempt in range(3):  # 最多 3 轮链式修复
                changed = False
                # 策略1：移除不存在的过滤字段（is_deleted 等幻觉）
                bad_fields = _extract_bad_fields(last_error if _attempt == 0 else "")
                if not bad_fields and _attempt == 0:
                    bad_fields = _extract_bad_fields(last_error)
                if bad_fields:
                    stripped = _auto_strip_invalid_filters(auto_fixed, bad_fields)
                    if stripped != auto_fixed:
                        auto_fixed = stripped
                        fixed_reasons.append(f"移除字段{bad_fields}")
                        changed = True
                # 策略2：歧义列加表限定（JOIN 时 id/create_time 等同名字段）
                qualified = _auto_qualify_ambiguous_columns(auto_fixed, datasource_id)
                if qualified != auto_fixed:
                    auto_fixed = qualified
                    fixed_reasons.append("歧义列加表限定")
                    changed = True
                if not changed:
                    break
                # 修复后重新校验
                try:
                    auto_fixed = _run_validations(auto_fixed, datasource_id, question, dialect)
                    logger.info("[NL2SQL] 确定性自动修复成功: %s, 校验通过", " + ".join(fixed_reasons))
                    yield {"type": "result", "result": {
                        "intent": "query", "sql": auto_fixed,
                        "explain": content, "tables": [t.table_name for t in tables],
                        "selected_tables": select_meta.get("raw", []),
                        "select_source": select_meta.get("source", ""),
                        "rag_hits": _rag_hits}}
                    return
                except Exception as retry_exc:  # noqa: BLE001
                    last_error = str(retry_exc)
                    continue
            logger.info("[NL2SQL] 确定性自动修复后仍校验失败，回退 LLM 重试")
            yield {"type": "retry", "msg": "SQL 校验中，正在修正…"}
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user",
                             "content": f"SQL 校验失败：{last_error}。请仅修正报错内容，**保持用户问题的查询语义不变**"
                                         "（项目/时间/状态等过滤条件、分组维度、计数/聚合形态都不得删减或改变）。"
                                         "修正后用 ```sql 代码块重新输出完整的 SELECT SQL；SQL 只能使用【可用表与字段】中列出的确切表名和字段名。"
                                         "常见修正：①字段不存在（如 is_deleted）→ 直接删除该过滤条件，不要替换为其他字段；"
                                         "②列歧义（ambiguous column）→ 给该字段加表别名限定（如 t1.create_time），所有子句都要限定；"
                                         "③CTE 别名作用域 → 外层查询使用 CTE 输出的别名列名，不要引用 CTE 内部原始列名；"
                                         "④表别名重复 → 每个表/子查询使用唯一别名。"})

    # LLM 已配置但连续生成失败：抛错误，不返回与问题无关的降级假数据
    if llm is not None:
        raise LLMError(f"SQL 生成连续失败：{last_error}")
    mock = _mock_sql(datasource_id, question, dialect)
    if mock.get("intent") == "query":
        mock["explain"] = (f"[降级模式] LLM 生成失败（{last_error[:80]}），"
                           f"已按关键词匹配返回候选表前 100 行，建议换种问法重试。")
        yield {"type": "result", "result": mock}
        return
    raise LLMError(f"SQL 生成连续失败：{last_error}")


def generate_sql_stream(datasource_id: int, workspace_id: int, question: str,
                        history: list[dict] | None = None, llm: LLMClient | None = None,
                        retries: int | None = None,
                        spec_context: dict | None = None,
                        schema_name: str | None = None,
                        exec_error: str | None = None,
                        table_hints: list[str] | None = None):
    """流式生成 SQL；yield 事件：
    - {"type": "stream", "delta": "..."}  LLM 输出逐块（打字机效果）
    - {"type": "retry", "msg": "..."}     校验失败正在重试
    - {"type": "result", "result": {...}}  最终结果（与 generate_sql 返回结构一致）
    逻辑与 generate_sql 完全一致，仅 LLM 调用改为流式。
    spec_context：AI 问数重构后由意图层传入 {"spec": {...}, "mapping": {...}, "plan": {...}}，
    LLM 退化为"翻译器"，只能按结构化参数生成 SQL（模板无法覆盖时的兜底路径）；
    detail 意图且未指定指标/字段时放宽为"从可用表与字段中按问题检索展示字段"。
    schema_name：查询范围 schema（项目/库），未显式传入时取 spec_context.spec.schema_name。"""
    from collections.abc import Generator
    db = SessionLocal()
    try:
        ds = db.query(Datasource).get(datasource_id)
        if not ds:
            raise LLMError("数据源不存在")
        dialect = ds.type
        # schema 限定：显式参数 > spec 上下文
        if not schema_name and spec_context:
            schema_name = (spec_context.get("spec") or {}).get("schema") or None
        # 拆表检索词：问题重构提取（table_hints），用于选表阶段表名/表注释预筛
        # 优先级：外部显式传入 > spec_context 内置
        _hints = table_hints or ((spec_context or {}).get("spec") or {}).get("table_hints") or []
        _spec_d = (spec_context or {}).get("spec") or {}
        _target = []
        for _m in (_spec_d.get("metrics") or []) + (_spec_d.get("dimensions") or []):
            _n = _m.get("name")
            if _n and _n not in _target:
                _target.append(_n)
        try:
            tables, select_meta = _llm_select_tables(
                datasource_id, question, schema_name=schema_name,
                llm=llm, history=history, table_hints=_hints,
                target_fields=_target)
        except TableSelectError as exc:
            # 回退为表级澄清：候选优先 = 拆表检索词命中表（收窄），无命中才全表
            logger.warning("[问数][选表] LLM 选表失败，回退表候选澄清: %s", exc)
            cand = []
            try:
                db2 = SessionLocal()
                try:
                    hits = _table_hint_hits(db2, datasource_id, schema_name, _hints)
                    if hits:
                        cand = [{"table": t.table_name, "comment": t.comment or ""}
                                for t in hits[:60]]
                    else:
                        all_t = _all_business_tables(db2, datasource_id, schema_name)
                        cand = [{"table": t.table_name, "comment": t.comment or ""}
                                for t in all_t]
                finally:
                    db2.close()
            except Exception as e2:  # noqa: BLE001
                logger.warning("[问数][选表] 读取全表候选失败: %s", e2)
            # 拆表检索词命中的表置顶，用户一眼可见最相关候选
            if cand and _hints:
                cand.sort(key=lambda c: _hint_hit_index(c["table"], c.get("comment") or "", _hints))
                _top = [c["table"] for c in cand[:5]
                        if _hint_hit_index(c["table"], c.get("comment") or "", _hints) < 99]
                if _top:
                    logger.info("[问数][选表] 拆表检索词命中候选置顶: %s", _top)
            # 关系图收窄候选表：过滤噪声表 + 事实表置顶 + top 8
            try:
                from .id_resolver import narrow_candidate_tables
                cand = narrow_candidate_tables(datasource_id, cand, question=question)
            except Exception:  # noqa: BLE001
                pass
            logger.info("[问数][选表] 表澄清候选 %d 张: %s", len(cand),
                        [c["table"] for c in cand][:20])
            yield {"type": "result", "result": {
                "intent": "clarify", "sql": "",
                "explain": f"未能自动识别与问题相关的数据表（{exc}）。请在下方勾选需要查询的表（可多选，将分别查询）：",
                "tables": [c["table"] for c in cand],
                "candidates": cand, "original_question": question,
                "table_hints": _hints}}
            return
        select_meta["raw"] = [{"id": t.id, "table_name": t.table_name, "comment": t.comment or ""}
                              for t in tables]
        # V1.1：相近表≥2 且问题无明确限定词 → 先澄清（主链路同样拦截，不再被 spec_context 跳过）
        # 用户已点选确认表（source=confirmed）时不再重复澄清——确认表是用户明确选择，不存在歧义
        is_confirmed = select_meta.get("source") == "confirmed"
        clarify = None if is_confirmed else _detect_clarify(question, datasource_id, tables)
        if clarify:
            cand_text = "；".join(f"{c['table']}（{c['comment'] or '无注释'}）" for c in clarify)
            yield {"type": "result", "result": {
                "intent": "clarify", "sql": "",
                "explain": f"您的问题对应多张表，请勾选需要查询的表（可多选，将分别查询）：{cand_text}",
                "tables": [c["table"] for c in clarify],
                "candidates": clarify, "original_question": question}}
            return
        # spec 已映射字段（mapping）与检索命中字段（hint_evidence）→ 优先推荐 + schema 分层
        _preferred: set[str] = set()
        _recommend_lines: list[str] = []
        _mapping = (spec_context or {}).get("mapping") or {}
        for _m in (_mapping.get("metrics") or []) + (_mapping.get("dimensions") or []):
            col = _m.get("column") or ""
            if col:
                _preferred.add(col)
                _recommend_lines.append(
                    f"- {_m.get('name') or col} → {col}（{_m.get('comment') or '无注释'}）")
        _evidence = select_meta.get("hint_evidence") or {}
        _by_tid = {t.id: t.table_name for t in tables}
        _evidence_lines: list[str] = []
        if _evidence:
            for _tid, _cols in _evidence.items():
                if _tid not in _by_tid:
                    continue
                for _c, _cm in _cols:
                    if _c:
                        _preferred.add(_c)
                _evidence_lines.append(
                    f"- {_by_tid[_tid]}: " + "、".join(
                        f"{c}（{cm or '无注释'}）" for c, cm in _cols[:4]))
        schema = (_schema_text(datasource_id, tables, schema_name, preferred=_preferred)
                  if tables else "（未匹配到相关数据表，说明当前问题不属于数据查询范畴）")
        joins = _join_paths_text(datasource_id, tables)
        evidence_section = (
            "\n【检索证据（拆表词/字段注释命中依据，供参考选表理由）】\n"
            + "\n".join(_evidence_lines)
            if _evidence_lines else "")
        recommend_section = (
            "\n【优先推荐字段（用户问题解析出的目标字段，★字段建议优先使用，可结合语义微调）】\n"
            + "\n".join(_recommend_lines)
            if _recommend_lines else "")
        # 用户已确认表（多轮澄清点选）：强制大模型综合考虑所有确认表，不得遗漏
        confirmed_section = ""
        if select_meta.get("source") == "confirmed" and len(tables) >= 2:
            confirmed_names = "、".join(t.table_name for t in tables)
            confirmed_section = f"""
【用户已确认选择的表（多轮澄清中明确勾选，共{len(tables)}张）】
{confirmed_names}
生成 SQL 时必须综合考虑以上所有确认表，不得遗漏任何一张（除非该表与问题完全无关，须在注释中说明原因）。

【场景一：各确认表统计同一指标的不同业务类型（如不同类型的用例/订单/记录）——最常用】
必须严格按以下模板生成（禁止用一个统计CTE去LEFT JOIN另一个统计CTE，会导致独有维度丢失）：
WITH stat_a AS (
    SELECT 表A.维度外键 AS dim_key, COUNT(*) AS cnt_a
    FROM 表A WHERE 表A.is_deleted = 0 GROUP BY 表A.维度外键
),
stat_b AS (
    SELECT 表B.维度外键 AS dim_key, COUNT(*) AS cnt_b
    FROM 表B WHERE 表B.is_deleted = 0 GROUP BY 表B.维度外键
)
SELECT
    dim.name AS 维度名称,
    COALESCE(stat_a.cnt_a, 0) AS 类型A数量,
    COALESCE(stat_b.cnt_b, 0) AS 类型B数量
FROM 维度主表 dim
LEFT JOIN stat_a ON stat_a.dim_key = dim.id
LEFT JOIN stat_b ON stat_b.dim_key = dim.id
WHERE dim.is_deleted = 0
ORDER BY 类型A数量 DESC, 类型B数量 DESC;
关键规则：
- CTE 必须按「维度外键」（如 project_id）聚合，禁止按名称聚合；
- 外层必须以「维度主表」（如 test_projects）为 FROM 主体，LEFT JOIN 各统计 CTE；
- 关联条件用 id（dim.id = stat.dim_key），禁止用名称关联；
- 禁止 FULL OUTER JOIN（MySQL 不支持）；禁止 stat_a LEFT JOIN stat_b（会丢失 stat_b 独有的维度）。

【场景二：表间存在外键关联且需联合过滤】
使用标准 JOIN...ON 关联查询，关联条件严格遵循主键-外键。

【场景三：UNION ALL】
仅在各 SELECT 列数、列类型完全一致时使用，外层只引用第一个 SELECT 的列别名；列数不一致时禁止使用（会报 1222 错误）。
"""
        examples = retrieve_few_shots(workspace_id, question)
        example_text = "\n".join(
            f"问题：{e['question']}\nSQL：{e['sql']}" for e in examples) or "（暂无示例）"
        from .rag import retrieve_doc_context
        doc_context = retrieve_doc_context(workspace_id, question)
    finally:
        db.close()

    history_text = ""
    if history:
        lines = []
        for m in history[-6:]:
            role_label = "用户" if m["role"] == "user" else "助手"
            line = f"{role_label}: {m.get('text', '')}"
            prev_sql = m.get("sql") or ""
            if prev_sql:
                line += f"\n  [上一次SQL] {prev_sql}"
            lines.append(line)
        history_text = "\n".join(lines)

    from .rag import hybrid_search
    rag_faq = hybrid_search(workspace_id, question, kind="faq", top_k=5, min_score=0.65)
    rag_doc = hybrid_search(workspace_id, question, kind="doc", top_k=5, min_score=0.5)
    _rag_hits = {"faq": rag_faq, "doc": rag_doc}
    rag_faq_text = "\n".join(
        f"- Q：{h['content'].split(chr(10))[0][3:]}\n  A：{chr(10).join(h['content'].split(chr(10))[1:]).lstrip('A：')}"
        if h.get('content') else ""
        for h in rag_faq) or "（无命中）"
    rag_doc_text = "\n".join(f"- {h['content'][:400]}" for h in rag_doc) or "（无命中）"

    spec_section = ""
    if spec_context:
        import json as _json
        spec_data = spec_context.get("spec", {})
        # 相对时间（近7天/昨天/本月等）：提示词中不展示解析出的绝对日期（start/end），
        # 避免 LLM 照抄字面量；仅保留相对表达，配合下方【时间过滤】要求推导
        _time = spec_data.get("time") or {}
        _time_expr = str(_time.get("expr") or "").strip()
        if _is_relative_time_expr(_time_expr) and _time.get("start"):
            _spec_display = dict(spec_data)
            _spec_display["time"] = {**_time, "start": "（由当前日期推导）", "end": "（由当前日期推导）"}
            _spec_json = _json.dumps(_spec_display, ensure_ascii=False)
        else:
            _spec_json = _json.dumps(spec_data, ensure_ascii=False)
        spec_section = (
            "\n【已解析的查询参数（必须严格遵循，禁止偏离）】\n"
            + _spec_json
            + "\n【字段映射结果（只能使用这些表和字段）】\n"
            + _json.dumps(spec_context.get("mapping", {}), ensure_ascii=False))
        # 相对时间：强制参数化，禁止把解析出的绝对日期写成字面量
        if _is_relative_time_expr(_time_expr):
            spec_section += (
                "\n【时间过滤（相对时间必须参数化）】\n"
                f"- 用户时间表达「{_time_expr}」是相对时间，必须用数据库当前日期函数（CURDATE()/NOW() + DATE_SUB/DATE_ADD）动态推导区间，**禁止**把 start/end 中的具体日期写成固定字面量（如 '2026-09-01 00:00:00'）；\n"
                "- 参考写法：过去N天/近N天=含今天在内的最近N个自然日 → `时间列 >= DATE_SUB(CURDATE(), INTERVAL (N-1) DAY) AND 时间列 < DATE_ADD(CURDATE(), INTERVAL 1 DAY)`（过去7天即 INTERVAL 6 DAY，终点为明天 0 点，**禁止**写成 `< CURDATE()` 否则漏掉今天）；昨天 → `时间列 >= DATE_SUB(CURDATE(), INTERVAL 1 DAY) AND 时间列 < CURDATE()`；本月 → `时间列 >= DATE_FORMAT(CURDATE(), '%Y-%m-01') AND 时间列 < DATE_ADD(DATE_FORMAT(CURDATE(), '%Y-%m-01'), INTERVAL 1 MONTH)`；\n"
                "- 区间左闭右开：起点取当天 0 点，终点取截止日次日 0 点；start/end 仅供核对口径（天数/月数），不得直接用作 SQL 字面量。"
            )
        # 分箱维度（范围分布，如"文件大小范围"）：必须生成 CASE WHEN 直方图 SQL
        _buckets = [d for d in (spec_context.get("mapping") or {}).get("dimensions", [])
                    if d.get("bucket")]
        if _buckets:
            _buckets_json = _json.dumps([{
                "维度": d.get("name"), "分箱字段": d["bucket"].get("field"),
                "区间": d["bucket"].get("ranges"), "标签": d["bucket"].get("labels"),
                "单位": d["bucket"].get("unit", ""),
            } for d in _buckets], ensure_ascii=False)
            spec_section += (
                f"\n【分箱维度（范围分布，必须严格按此生成，禁止改写边界）】\n{_buckets_json}\n"
                "- 对每个分箱维度生成 `CASE WHEN 分箱字段 >= 下界 AND 分箱字段 < 上界 THEN '区间标签' ... ELSE '其他' END` 作为 SELECT 列，AS 中文维度名；\n"
                "- 最后一个区间若标签语义为「以上/超过」，用 `分箱字段 >= 下界`（无上界）；\n"
                "- 分组使用 `GROUP BY` 该表达式（可直接写 GROUP BY 1，多分组维度时按 SELECT 顺序编号）；\n"
                "- 边界值与标签必须逐字来自上述定义，禁止自行编造区间；分箱字段名必须是映射结果中的确切字段。")
        # 计数类指标：提示 LLM 统计执行/调用记录表，避免对定义表 COUNT（每行恒为 1）
        _cnt_names = [m.get("name", "") for m in (spec_data.get("metrics") or [])]
        if any(any(k in n for k in ("次数", "调用", "访问", "请求")) for n in _cnt_names):
            spec_section += ("\n【计数类指标提示】\n"
                             "- 该指标需统计「发生记录」表的行数（COUNT(*)），如执行记录/调用记录/日志表；\n"
                             "- 禁止对「定义/配置/字典」类表做 COUNT（每行恒为 1，无统计意义）；\n"
                             "- 按用户问题的业务主体选择正确的统计表。")
        # 明细/清单类且未指定指标/字段：放宽为"按问题检索展示字段"（R7 字段检索）
        if (spec_data.get("intent") == "detail"
                and not spec_data.get("metrics")
                and not spec_data.get("dimensions")):
            spec_section += ("\n【本查询为明细/清单类，未指定展示字段】\n"
                             "- 请根据用户问题从【可用表与字段】中检索语义最匹配的表与字段作为 SELECT 列；\n"
                             "- 表名、字段名必须逐字来自【可用表与字段】，禁止编造；\n"
                             "- 过滤条件按问题语义使用【可用表与字段】中的字段作 WHERE；\n"
                             "- 用户未指定时间时，不得添加任何时间过滤条件。")

    user_prompt = f"""【历史对话】
{history_text or "（无）"}
{confirmed_section}
【可用表与字段（格式：表名: 字段(类型·注释)，★=优先推荐字段，已按选中表过滤）】
{schema}
{recommend_section}
{evidence_section}

【JOIN 路径】
{joins}

【相似示例】
{example_text}

【知识库 FAQ 命中】（与用户问题一致或高度相似才列出）
{rag_faq_text}

【知识库文档命中】（可直接回答用户问题才列出）
{rag_doc_text}
{spec_section}

【判定规则】
- 用户问题与【知识库 FAQ 命中】一致/高度相似，或【知识库文档命中】可直接回答 → intent=knowledge（sql 置空）；
- 否则需要查询数据库数据（【可用表与字段】有相关表）→ intent=query，给出 sql；
- 否则通用对话/创作/翻译/写作/编程等不依赖数据库的任务 → intent=chat（explain 放完整回答）；
- 否则恶意请求/违法内容/完全无意义 → intent=refuse。
- 判断为 query 时，直接输出一个完整闭合的 ```sql 代码块（SELECT），不要输出 intent 标记；
- 判断为 chat/knowledge/refuse 时，第一行必须独占输出 `intent=chat` / `intent=knowledge` / `intent=refuse` 标记，第二行起为自然语言回答，**禁止**为了凑 SQL 而编造不存在的表或字段。

【用户问题】{question}

数据查询严格按系统提示词「四、输出格式」仅输出一个完整闭合的 ```sql 代码块（含必要注释），无多余文字；非数据查询按「五、判定规则」首行输出 intent 标记后再写回答。"""
    if exec_error:
        user_prompt += f"""
【上次执行失败，必须修正】
{exec_error}
请分析错误原因并修正 SQL：**保持用户问题的查询语义不变**（项目/时间/状态等过滤条件、分组维度、计数/聚合形态都不得删减或改变），只修正报错本身；只使用【可用表与字段】中确切存在的表名和字段；聚合查询中 GROUP BY 必须包含 SELECT 中全部非聚合列（注意 only_full_group_by 模式）；若错误为 Subquery returns more than 1 row，必须把 `= (SELECT ...)` 改为 `IN (SELECT ...)`；若错误为 Column 'xxx' in field list is ambiguous（列名歧义），必须为 SELECT、ORDER BY、WHERE、GROUP BY 中的重名列显式加上表别名限定（如 stat_a.xxx、stat_b.xxx），并保证 JOIN 条件与 SELECT 列使用同一别名；若错误为 CTE 作用域错误（某字段已在 CTE 中被别名化），必须将后续查询中对该原始列名的引用全部替换为 CTE 输出的别名；若错误为字段不存在（尤其是 is_deleted 等软删字段），必须直接删除 SQL 中所有对该字段的引用（如 WHERE 条件），不要尝试用其他字段替代；若错误为 Not unique table/alias（表别名重复），必须为 FROM/JOIN 中的每个表分配唯一别名（如 t1、t2、sub_a、sub_b），并同步更新 SELECT/WHERE/GROUP BY/ORDER BY/JOIN ON 中对该别名的所有列引用；修正后**仅输出**修正后的 ```sql 代码块（含必要注释）。"""

    client = llm
    if client is None or not client.configured:
        yield {"type": "result", "result": _mock_sql(datasource_id, question, dialect)}
        return

    from .sql_extractor import extract_first_sql
    from ..executor import validate_sql
    messages = [{"role": "system", "content": _load_sql_system_prompt()},
                {"role": "user", "content": user_prompt}]
    from ..config import get_settings
    attempts = (retries if retries is not None else get_settings().sql_correct_retries)
    last_error = ""

    for i in range(attempts + 1):
        # 流式调用 LLM（自然语言 + markdown SQL；关闭内部 thinking 避免 Qwen3.5-4B 思考链挤占 content）
        content = ""
        stream_gen = client.chat_stream(messages, json_mode=False, max_tokens=4096, thinking=False)
        while True:
            try:
                chunk = next(stream_gen)
                # 兼容多种 chunk 载荷：dict（content/reasoning）或纯文本 str（部分 SDK 直出 delta）
                if isinstance(chunk, dict):
                    c_delta = chunk.get("content", "")
                    r_delta = chunk.get("reasoning", "")
                elif isinstance(chunk, str):
                    c_delta, r_delta = chunk, ""
                else:
                    c_delta, r_delta = str(chunk), ""
                content += c_delta
                if r_delta:
                    yield {"type": "thinking", "delta": r_delta}
                if c_delta:
                    yield {"type": "stream", "delta": c_delta}
                    # 提前中断1：检测到完整的 ```sql ... ``` 代码块就停止
                    if "```sql" in content and content.rfind("```") > content.find("```sql") + 5:
                        break
            except StopIteration as e:
                val = e.value or {}
                content = (val.get("content") or content).strip()
                break
        # 从回答中提取 SQL（markdown ```sql 代码块优先，兜底裸 SELECT）
        sql = _normalize_sql_punctuation(extract_first_sql(content) or "")
        # 排查辅助：每次 LLM 响应全文打印到日志（含提取结果）
        logger.info("[NL2SQL] 第 %d 次尝试, 提取=%s\n-----响应原文-----\n%s\n-----END-----",
                    i + 1, "OK" if sql else "未提取到SQL", content)
        if not sql:
            non_sql = _detect_non_sql_answer(content)
            if non_sql:
                # 模型判定非数据查询（chat/knowledge/refuse）：直接返回文字回答，不重试
                logger.info("[NL2SQL] 判定为 %s 回答，跳过 SQL 重试", non_sql)
                yield {"type": "result", "result": {
                    "intent": non_sql, "sql": "", "explain": _strip_intent_marker(content),
                    "tables": [t.table_name for t in tables],
                    "selected_tables": select_meta.get("raw", []),
                    "select_source": select_meta.get("source", ""),
                    "rag_hits": _rag_hits}}
                return
            last_error = "未从回答中提取到 SELECT SQL 代码块"
            # 带诊断的重试：指出上一轮具体问题，让重试有增量信息而非空转
            if "```" in content and "```sql" not in content:
                _diag = "上一次回答用了代码块但语言标记不是 sql（必须以 ```sql 开头）"
            elif "select" not in content.lower():
                _diag = "上一次回答是自然语言、完全没有 SELECT 语句；若确实无表/字段可用，按协议首行输出 intent=refuse，否则必须给出 SQL"
            else:
                _diag = "上一次回答中的 SQL 未被完整识别：必须以 ```sql 开头、以 ``` 闭合，代码块内是完整可执行的 SELECT"
            yield {"type": "retry", "msg": "未识别到 SQL，正在重试…"}
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user",
                             "content": f"【重要】{_diag}。请严格按系统提示词「四、输出格式」：**仅输出** 一个完整闭合的 ```sql 代码块（含必要注释），无任何多余文字，不要重复上一次的错误。"})
            continue

        # 校验 SQL（语法/只读/表存在/字段存在/计数形状），通过则应用中文字段别名
        logger.info("[NL2SQL] 提取后SQL前300字: %s", sql[:300])
        try:
            sql = _run_validations(sql, datasource_id, question, dialect)
            yield {"type": "result", "result": {
                "intent": "query", "sql": sql,
                "explain": content, "tables": [t.table_name for t in tables],
                "selected_tables": select_meta.get("raw", []),
                "select_source": select_meta.get("source", ""),
                "rag_hits": _rag_hits}}
            return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            logger.warning("[NL2SQL] 校验失败原因: %s | 异常类型: %s", last_error[:200], type(exc).__name__)
            # 确定性自动修复（链式）：依次尝试多种修复策略，全部失败才回退 LLM 重试
            auto_fixed = sql
            fixed_reasons: list[str] = []
            for _attempt in range(3):  # 最多 3 轮链式修复
                changed = False
                # 策略1：移除不存在的过滤字段（is_deleted 等幻觉）
                bad_fields = _extract_bad_fields(last_error if _attempt == 0 else "")
                if not bad_fields and _attempt == 0:
                    bad_fields = _extract_bad_fields(last_error)
                if bad_fields:
                    stripped = _auto_strip_invalid_filters(auto_fixed, bad_fields)
                    if stripped != auto_fixed:
                        auto_fixed = stripped
                        fixed_reasons.append(f"移除字段{bad_fields}")
                        changed = True
                # 策略2：歧义列加表限定（JOIN 时 id/create_time 等同名字段）
                qualified = _auto_qualify_ambiguous_columns(auto_fixed, datasource_id)
                if qualified != auto_fixed:
                    auto_fixed = qualified
                    fixed_reasons.append("歧义列加表限定")
                    changed = True
                if not changed:
                    break
                # 修复后重新校验
                try:
                    auto_fixed = _run_validations(auto_fixed, datasource_id, question, dialect)
                    logger.info("[NL2SQL] 确定性自动修复成功: %s, 校验通过", " + ".join(fixed_reasons))
                    yield {"type": "result", "result": {
                        "intent": "query", "sql": auto_fixed,
                        "explain": content, "tables": [t.table_name for t in tables],
                        "selected_tables": select_meta.get("raw", []),
                        "select_source": select_meta.get("source", ""),
                        "rag_hits": _rag_hits}}
                    return
                except Exception as retry_exc:  # noqa: BLE001
                    last_error = str(retry_exc)
                    continue
            logger.info("[NL2SQL] 确定性自动修复后仍校验失败，回退 LLM 重试")
            yield {"type": "retry", "msg": "SQL 校验中，正在修正…"}
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user",
                             "content": f"SQL 校验失败：{last_error}。请仅修正报错内容，**保持用户问题的查询语义不变**"
                                         "（项目/时间/状态等过滤条件、分组维度、计数/聚合形态都不得删减或改变）。"
                                         "修正后用 ```sql 代码块重新输出完整的 SELECT SQL；SQL 只能使用【可用表与字段】中列出的确切表名和字段名。"
                                         "常见修正：①字段不存在（如 is_deleted）→ 直接删除该过滤条件，不要替换为其他字段；"
                                         "②列歧义（ambiguous column）→ 给该字段加表别名限定（如 t1.create_time），所有子句都要限定；"
                                         "③CTE 别名作用域 → 外层查询使用 CTE 输出的别名列名，不要引用 CTE 内部原始列名；"
                                         "④表别名重复 → 每个表/子查询使用唯一别名。"})

    # LLM 已配置但连续生成失败：抛错误，不返回与问题无关的降级假数据
    if llm is not None:
        raise LLMError(f"SQL 生成连续失败：{last_error}")
    mock = _mock_sql(datasource_id, question, dialect)
    if mock.get("intent") == "query":
        mock["explain"] = (f"[降级模式] LLM 生成失败（{last_error[:80]}），"
                           f"已按关键词匹配返回候选表前 100 行，建议换种问法重试。")
        yield {"type": "result", "result": mock}
        return
    raise LLMError(f"SQL 生成连续失败：{last_error}")


def _mock_sql(datasource_id: int, question: str, dialect: str) -> dict:
    """无 LLM 配置时的演示 SQL：从候选表取前 N 行（明确标注演示模式）。"""
    db = SessionLocal()
    try:
        tables = retrieve_candidate_tables(datasource_id, question, top_k=1)
        if not tables:
            return {"intent": "refuse", "sql": "",
                    "explain": "未匹配到相关数据表（演示模式）", "tables": []}
        t = tables[0]
        cols = (db.query(ColumnMeta).filter(ColumnMeta.table_meta_id == t.id)
                .order_by(ColumnMeta.ordinal).limit(6).all())
        if not cols:
            return {"intent": "refuse", "sql": "", "explain": "表中无字段", "tables": []}
        col_sql = ", ".join(c.column_name for c in cols)
        sql = f"SELECT {col_sql} FROM {t.table_name} LIMIT 100"
        return {"intent": "query",
                "sql": sql,
                "explain": f"演示模式：未配置大模型，已按关键词匹配「{t.comment or t.table_name}」表返回前 100 行；请在「模型配置」添加 LLM 后自动生成业务 SQL",
                "tables": [t.table_name]}
    finally:
        db.close()


def _check_tables_exist(sql: str, datasource_id: int) -> None:
    """校验 SQL 引用的表均存在于已采集 Schema（datasource_id 下，含 schema 前缀匹配）。"""
    import sqlglot
    from ..database import SessionLocal
    from ..models import TableMeta
    from ..executor import SqlExecError
    ast = sqlglot.parse_one(sql, read="mysql")
    cte_names = {c.alias for c in ast.find_all(sqlglot.exp.CTE) if c.alias}
    refs = set()
    for node in ast.find_all(sqlglot.exp.Table):
        if node.name and node.name not in cte_names:
            refs.add((node.catalog or "", node.db or "", node.name))
    if not refs:
        return
    db = SessionLocal()
    try:
        known = {(t.schema_name or "", t.table_name)
                 for t in db.query(TableMeta)
                 .filter(TableMeta.datasource_id == datasource_id).all()}
    finally:
        db.close()
    known_names = {t for _s, t in known}
    for _c, sch, tbl in refs:
        if sch:
            ok = (sch, tbl) in known
        else:
            ok = tbl in known_names  # 无 schema 前缀：按表名匹配
        if ok:
            continue
        raise SqlExecError(f"表 {sch + '.' if sch else ''}{tbl} 不存在于已采集 Schema，请使用可用表中的确切表名")


def _check_columns_exist(sql: str, datasource_id: int) -> None:
    """校验 SQL 引用的字段均存在于对应表（防 LLM 幻觉列，如 enabled 不存在的字段）。
    规则：
    - 表限定列（t.col）：严格校验该表存在该列；
    - 未限定列：只要引用表中至少一张表存在该列即放行（联表场景列归属不明）；
      若所有引用表都没有 → 判定为幻觉列，报错触发重试。"""
    import sqlglot
    import sqlglot.expressions as exp
    from ..database import SessionLocal
    from ..models import ColumnMeta, TableMeta
    from ..executor import SqlExecError

    ast = sqlglot.parse_one(sql, read="mysql")
    # CTE 名与 CTE 输出列（WITH 子句内部列，不属于任何真实表）
    cte_names = {c.alias for c in ast.find_all(exp.CTE) if c.alias}
    cte_cols: set[str] = set()
    for cte in ast.find_all(exp.CTE):
        for al in cte.this.find_all(exp.Alias):
            if al.alias:
                cte_cols.add(al.alias)
    # 全部 SELECT 别名（顶层/子查询/CTE）：ORDER BY/GROUP BY 引用别名时不当作字段校验
    select_aliases: set[str] = set()
    for al in ast.find_all(exp.Alias):
        if al.alias:
            select_aliases.add(al.alias)
    # 引用表（含别名 → 表名/CTE 名）；CTE 名不参与真实表校验，但别名映射需收录（JOIN CTE 别名 p）
    alias_map: dict[str, str] = {}
    for node in ast.find_all(exp.Table):
        if node.name:
            alias_map[node.alias or ""] = node.name
    ref_tables = {n for n in alias_map.values() if n and n not in cte_names}
    for node in ast.find_all(exp.Table):
        if node.name and node.name not in cte_names:
            ref_tables.add(node.name)
    if not ref_tables:
        return
    db = SessionLocal()
    try:
        tmap = {t.table_name: t for t in db.query(TableMeta)
                .filter(TableMeta.datasource_id == datasource_id).all()}
        cols_of: dict[str, set[str]] = {}
        for tname, tm in tmap.items():
            cols_of[tname] = {c.column_name for c in db.query(ColumnMeta)
                              .filter(ColumnMeta.table_meta_id == tm.id).all()}
    finally:
        db.close()

    unknown: list[str] = []
    for node in ast.find_all(exp.Column):
        if isinstance(node.this, exp.Star):  # COUNT(*) 等
            continue
        cname = node.name or ""
        if not cname:
            continue
        tbl = node.table or ""
        if tbl:  # 表限定列：严格校验
            real = alias_map.get(tbl) or tbl
            if real in cte_names:
                continue  # CTE 限定列（如 rt_project_ids.id）属于 CTE，不校验
            if real in cols_of:
                if cname not in cols_of[real]:
                    unknown.append(f"{real}.{cname}")
            elif real not in ref_tables:
                # 限定列引用了既不存在的表名也不存在的别名（幻觉别名，如 FROM test_cases tc 却写 t.name）
                unknown.append(f"{real}.{cname}（表/别名 {real} 未引用）")
        else:  # 未限定列：CTE 输出列/SELECT 别名（ORDER BY 引用）放行；其余所有引用表都没有才报错
            if cname in cte_cols or cname in select_aliases:
                continue
            if ref_tables and not any(cname in cols_of.get(rt, set()) for rt in ref_tables):
                unknown.append(cname)
    if unknown:
        # 实时库交叉检查：元数据可能过期（缺列），对疑似缺失列查 information_schema 确认；
        # 实际表中存在的列从 unknown 中移除并补入 cols_of，避免误报触发无效重试
        from ..executor import get_table_columns
        live_verified: set[str] = set()
        for rt in ref_tables:
            live_cols = get_table_columns(datasource_id, rt)
            if live_cols:
                live_names = {c["column_name"] for c in live_cols}
                for u in list(unknown):
                    if u in live_names and u not in live_verified:
                        live_verified.add(u)
                        cols_of.setdefault(rt, set()).add(u)
        unknown = [u for u in unknown if u not in live_verified]
    if unknown:
        # 列出每个引用表的可用列名，帮助 LLM 一次修正（避免把注释当列名）
        hints = []
        for rt in sorted(ref_tables):
            if rt in cols_of:
                available = ", ".join(sorted(cols_of[rt]))
                hints.append(f"{rt} 可用列: {available}")
        hint_str = ("；" + "；".join(hints)) if hints else ""
        # 软删类字段（is_deleted 等）是高频幻觉列：表中没有时直接要求删除全部引用，
        # 避免模型改用其他字段替代或反复重试
        soft_cols = [u for u in sorted(set(unknown))
                     if u.lower() in ("is_deleted", "deleted", "delete_flag")]
        extra = ""
        if soft_cols:
            extra = ("；注意：以上表中不存在 " + "、".join(soft_cols)
                     + " 字段，请直接删除 SQL 中所有对该字段的引用（如 WHERE 条件），"
                       "不要尝试用其他字段替代")
        raise SqlExecError(
            "字段不存在：" + ", ".join(sorted(set(unknown)))
            + hint_str + extra
            + "；请使用上表中的确切字段名修正 SQL")


def _check_cte_alias_scope(sql: str) -> None:
    """CTE 别名作用域校验：CTE 中将原始列别名化后，后续查询必须使用别名，
    禁止再引用原始列名（否则触发「Unknown column」执行错误）。

    仅检测明确的别名映射（原始列 AS 别名），且该原始列未在同一 CTE 中直接选中；
    后续查询的 FROM/JOIN 中包含该 CTE 时，引用原始列名即报错。
    """
    import sqlglot
    import sqlglot.expressions as exp
    from ..executor import SqlExecError

    try:
        ast = sqlglot.parse_one(sql, read="mysql")
    except Exception:  # noqa: BLE001
        return  # 语法错误由其他校验器处理

    ctes = list(ast.find_all(exp.CTE))
    if not ctes:
        return

    # 收集每个 CTE 中「被别名化且未直接选中」的原始列 → 别名
    cte_aliased: dict[str, dict[str, str]] = {}
    for cte in ctes:
        if not cte.alias or not isinstance(cte.this, exp.Select):
            continue
        aliased: dict[str, str] = {}
        direct: set[str] = set()
        for item in (cte.this.expressions or []):
            if isinstance(item, exp.Alias) and isinstance(item.this, exp.Column):
                aliased[item.this.name] = item.alias or item.this.name
            elif isinstance(item, exp.Column):
                direct.add(item.name)
        # 仅保留「别名化且未直接选中」的列（直接选中的原始列仍在 CTE 输出中可用）
        cte_aliased[cte.alias] = {k: v for k, v in aliased.items() if k not in direct}

    if not any(cte_aliased.values()):
        return

    # 按定义顺序检查：每个 CTE 体可引用之前定义的 CTE；主查询可引用全部 CTE
    defined: set[str] = set()

    def _check_select(select: exp.Select, available_ctes: set[str],
                      skip_col_ids: set[int] | None = None) -> None:
        skip_col_ids = skip_col_ids or set()
        from_ctes = {t.name for t in select.find_all(exp.Table) if t.name in available_ctes}
        if not from_ctes:
            return
        # 当前查询 FROM 中涉及的 CTE 里，被别名化的原始列名 → 别名
        aliased_map: dict[str, str] = {}
        for cn in from_ctes:
            aliased_map.update(cte_aliased.get(cn, {}))
        if not aliased_map:
            return
        for col in select.find_all(exp.Column):
            if id(col) in skip_col_ids:
                continue
            cname = col.name or ""
            if not cname:
                continue
            # 表限定列：仅当表名是当前查询引用的 CTE 别名时才检查（实际表列交给字段存在性校验）
            if col.table and col.table not in from_ctes:
                continue
            if cname in aliased_map:
                raise SqlExecError(
                    f"CTE 作用域错误：字段 `{cname}` 已在 CTE 中被别名化为 `{aliased_map[cname]}`，"
                    f"后续查询必须使用别名 `{aliased_map[cname]}`，不能再引用原始列名 `{cname}`；"
                    f"请修正 SQL 中的字段引用。")

    for cte in ctes:
        if isinstance(cte.this, exp.Select):
            _check_select(cte.this, defined)
        if cte.alias:
            defined.add(cte.alias)

    # 外层查询：检查所有不在 CTE 定义内部的 Select 节点（含主查询、子查询、UNION 各分支）。
    # 收集 CTE 内部列节点 ID，避免外层 find_all 遍历进 CTE 定义造成误报。
    cte_select_ids = {id(cte.this) for cte in ctes if isinstance(cte.this, exp.Select)}
    cte_col_ids: set[int] = set()
    for cte in ctes:
        for col in cte.this.find_all(exp.Column):
            cte_col_ids.add(id(col))
    all_cte_names = set(cte_aliased.keys())
    for sel in ast.find_all(exp.Select):
        if id(sel) in cte_select_ids:
            continue
        _check_select(sel, all_cte_names, skip_col_ids=cte_col_ids)


def _check_ambiguous_columns(sql: str, datasource_id: int) -> None:
    """JOIN 多表时，WHERE/GROUP BY/ORDER BY/HAVING 中未限定的列若在多张表中都存在，
    执行时会报 ambiguous column。提前检测并报错，引导 LLM 加表限定。"""
    import sqlglot
    import sqlglot.expressions as exp
    from ..executor import SqlExecError, get_table_columns

    try:
        ast = sqlglot.parse_one(sql, read="mysql")
    except Exception:  # noqa: BLE001
        return

    # 收集主查询（含子查询）中所有 FROM/JOIN 的真实表
    all_tables: list[str] = []
    for sel in ast.find_all(exp.Select):
        for t in sel.find_all(exp.Table):
            name = t.name
            if name and name not in all_tables:
                all_tables.append(name)
    if len(all_tables) < 2:
        return  # 单表无歧义

    # 获取每张表的列集合（缓存）
    table_cols: dict[str, set[str]] = {}
    for tname in all_tables:
        try:
            cols = get_table_columns(datasource_id, tname)
            table_cols[tname] = {c["column_name"] for c in cols} if isinstance(cols, list) else set()
        except Exception:  # noqa: BLE001
            table_cols[tname] = set()

    # 对每个 Select 节点，检查其 WHERE/GROUP BY/ORDER BY/HAVING 中的未限定列
    for sel in ast.find_all(exp.Select):
        sel_tables = [t.name for t in sel.find_all(exp.Table)]
        if len(sel_tables) < 2:
            continue
        # 收集该查询中所有未限定列（col.table 为空），排除 SELECT 列表中的别名定义
        qualified_prefixes: set[str] = set()
        for col in sel.find_all(exp.Column):
            if col.table:
                qualified_prefixes.add(col.table)
        # 检查 WHERE/GROUP BY/ORDER BY/HAVING 中的未限定列
        clauses = []
        if sel.args.get("where"):
            clauses.append(sel.args["where"])
        if sel.args.get("group"):
            clauses.append(sel.args["group"])
        if sel.args.get("order"):
            clauses.append(sel.args["order"])
        if sel.args.get("having"):
            clauses.append(sel.args["having"])
        for clause in clauses:
            for col in clause.find_all(exp.Column):
                if col.table:
                    continue  # 已限定
                cname = col.name or ""
                if not cname:
                    continue
                # 检查该列在多少张表中存在
                present_in = [t for t in sel_tables if cname in table_cols.get(t, set())]
                if len(present_in) >= 2:
                    raise SqlExecError(
                        f"列歧义：字段 `{cname}` 在表 {present_in} 中都存在，"
                        f"必须加表限定（如 `{sel_tables[0]}.{cname}`）；"
                        f"请修正 SQL 中 WHERE/GROUP BY/ORDER BY/HAVING 的字段引用。")


def _check_id_display(sql: str, datasource_id: int) -> None:
    """外键 ID 展示校验：GROUP BY 中直接使用外键 ID 字段（以 _id 结尾），
    且存在关联表的名称字段、但 SQL 未 JOIN 关联表时，报错触发 LLM 修正为名称展示。
    仅检测最外层查询的 GROUP BY，避免对子查询/CTE 内部误报。"""
    import sqlglot
    import sqlglot.expressions as exp
    from ..executor import SqlExecError

    try:
        ast = sqlglot.parse_one(sql, read="mysql")
    except Exception:  # noqa: BLE001
        return

    # 找到最外层 SELECT（不在 CTE 内部）
    cte_select_ids = {id(c.this) for c in ast.find_all(exp.CTE) if isinstance(c.this, exp.Select)}
    outer = None
    for sel in ast.find_all(exp.Select):
        if id(sel) not in cte_select_ids:
            outer = sel
            break
    if outer is None:
        return

    group = outer.args.get("group")
    if not group:
        return

    # 收集 FROM/JOIN 中已引用的表
    used_tables = {t.name for t in outer.find_all(exp.Table)}

    # 收集 ID 关联提示（按 表名.列名 索引）
    try:
        from .id_resolver import get_id_display_hints
        hints = get_id_display_hints(datasource_id)
    except Exception:  # noqa: BLE001
        return
    hint_map: dict[tuple[str, str], dict] = {}
    for h in hints:
        hint_map[(h["src_table"], h["src_col"])] = h

    # 别名 → 表名映射
    alias_to_table: dict[str, str] = {}
    for t in outer.find_all(exp.Table):
        if t.alias:
            alias_to_table[t.alias] = t.name

    for col in group.find_all(exp.Column):
        cname = (col.name or "").lower()
        if not cname.endswith("_id") or cname == "id":
            continue
        # 确定该列所属的表
        owner_tables: list[str] = []
        if col.table:
            tname = alias_to_table.get(col.table, col.table)
            owner_tables = [tname]
        else:
            # 未限定列：可能属于任意已引用表
            owner_tables = list(used_tables)
        for tname in owner_tables:
            hint = hint_map.get((tname, col.name))
            if not hint:
                continue
            # 关联表已在 FROM/JOIN 中，视为已处理
            if hint["dst_table"] in used_tables:
                continue
            disp = hint["display_col"] or hint["dst_col"]
            raise SqlExecError(
                f"分组维度使用了外键 ID `{tname}.{col.name}`，"
                f"应 JOIN 关联表 `{hint['dst_table']}` "
                f"（ON {hint['dst_table']}.{hint['dst_col']} = {tname}.{col.name}），"
                f"并用 `{hint['dst_table']}.{disp}` 做展示/分组维度，"
                f"禁止直接用 ID 数值做统计展示。")
