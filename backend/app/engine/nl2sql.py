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

from ..database import SessionLocal
from ..llm import LLMClient, LLMError, parse_json_content
from ..models import ColumnMeta, Datasource, Relationship, SqlExample, TableMeta
from .text_utils import tokenize  # L-0 公共化：统一分词
from .schema_types import SYSTEM_TABLES, is_id_field, is_system_field  # L-0 公共化：类型/系统字段
from .biz_lexicon import match as lex_match  # L-0 公共化：业务词表
from . import prompt_kit  # L-0 公共化：prompt 拼装
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


# 相对时间表达（近7天/本月/昨天等）：命中时 SQL 必须参数化，禁止写死固定日期
_TIME_RELATIVE_RE = re.compile(
    r"(?:近|最近|过去|前)\s*\d{1,3}\s*(?:天|日|周|个?月)"
    r"|(?:上|本|这|当|今|下)\s*(?:个?月|个?季度|季度|季)"
    r"|(?:去|今|本|这|明)\s*年"
    r"|昨天|今天|前天|上周|本周|下周|上月|本月|下月"
)


def _is_relative_time_expr(expr: str) -> bool:
    return bool(expr and _TIME_RELATIVE_RE.search(expr))


SYSTEM_PROMPT = """你是企业数据问数助手，负责把用户中文问题转换为可执行、可直接运行的 SQL。

## 核心指令
请根据用户的**业务查询问题**，结合提供的【可用表与字段】（完整数据库表结构：表名/字段名/字段类型/注释）、【JOIN 路径】（表关联关系：主键/外键），严格匹配需求，生成**可直接运行、语法标准、逻辑严谨**的 SQL 语句。
**SQL 必须与用户问题语义严格一致**：用户询问数量/计数（如"…有多少/几个/几条/分别多少"）时，必须输出聚合查询（COUNT/SUM）并按分组字段 GROUP BY，**禁止**把计数问题写成 `SELECT ... LIMIT n` 的明细行查询；用户明确要明细/列表时才返回明细字段。

## 一、基础约束（强制）
1. 无表/无对应字段时：**仅输出**「未检索到相关表/字段，无法生成SQL语句」，不做任何假设；
2. 生成 SQL 时，**所有输出仅包含 SQL 代码 + 关键注释**，无任何多余文字、解释、说明（不要输出查询思路、不要分析过程、不要编号列举）；
3. 查询结果字段**必须全部转换为中文别名**（每一个 SELECT 列都要 `AS 中文别名`，明细查询同样强制，不允许出现英文字段名列头）；别名**仅取字段主名**：如字段注释为「项目ID（NULL=全局，预留项目级）」则 `AS 项目ID`；「状态：pending-等待，running-执行中」则 `AS 状态`。**禁止**把注释中的枚举值、括号说明、取值说明带入别名；
4. **ID 字段治理（强制）**：结果中**禁止直接展示纯标识类 ID 字段**（如 `id`、`project_id`、`version_id`、`module_id`、`api_id`、`created_by`、`user_id`、`environment_id`、`report_id` 等），除非用户问题明确要求"ID/编号"。凡业务上有对应名称表的，必须通过 JOIN 关联取名称列展示：`project_id` → JOIN 项目表取 `项目名称`、`version_id` → JOIN 版本表取 `版本名称`、`module_id` → JOIN 目录/模块表取 `目录名称`、`api_id` → JOIN 接口定义表取 `接口名称`、`created_by/user_id` → JOIN 用户表取 `创建人姓名`。关联依据严格使用【JOIN 路径】中的主键-外键；若【JOIN 路径】无对应关系且字段注释也未指明关联表，才允许保留该 ID 列（仍须中文别名）。明细查询同样适用本规则。
5. SQL 格式：**Markdown 代码块**包裹，语言标记为 `sql`，缩进规范、排版工整；代码块必须完整闭合（```sql 开头、``` 结尾），代码块内只能有 SQL 与注释。

## 二、表关联规范（强制）
1. 多表查询**必须使用标准 JOIN...ON**，**禁止**使用逗号分隔表的旧式关联写法；
2. 关联类型精准匹配业务：
   - 需匹配双方存在数据：`INNER JOIN`
   - 保留左表全部数据，右表匹配：`LEFT JOIN`
   - 保留右表全部数据，左表匹配：`RIGHT JOIN`
3. 关联条件**严格遵循【JOIN 路径】提供的主键-外键关联**，无自定义、无错误关联；【JOIN 路径】中未出现的表间关系禁止自行假设。

## 三、语法编写规范（强制）
1. 表/字段必须使用**简洁易懂的别名**，字段引用无歧义（多表同名字段必须加表别名）；
2. 复杂查询**优先使用 CTE(WITH子句)** 拆分业务逻辑，禁止嵌套过深；
3. 支持语法：子查询、`GROUP BY`/`HAVING`、`ORDER BY`、`LIMIT`(分页)、`WHERE`、`DISTINCT`、聚合函数(`SUM/COUNT/AVG/MAX/MIN`)、条件判断；
4. 代码要求：**简洁高效、无冗余逻辑、无语法错误**；
5. 严格贴合需求：**不新增无关条件、不返回多余字段、不修改业务逻辑**。

## 四、输出格式（强制）
```sql
-- 关键业务注释（可选，核心逻辑标注）
WITH 自定义CTE别名 AS (  -- 复杂查询必用
    -- CTE子查询
)
SELECT 
    字段1 AS 中文别名,
    字段2 AS 中文别名,
    聚合函数(字段) AS 中文别名
FROM 主表 表别名
JOIN 关联表 表别名 ON 主表主键 = 关联表外键
WHERE 过滤条件
GROUP BY 分组字段
HAVING 分组过滤条件
ORDER BY 排序字段 DESC/ASC
LIMIT 分页参数;
```

## 五、判定规则
- 用户问题与【知识库 FAQ 命中】一致/高度相似，或【知识库文档命中】可直接回答 → intent=knowledge（sql 置空，输出文字回答）；
- 否则需要查询数据库数据（【可用表与字段】有相关表）→ intent=query，必须按上述格式输出完整可执行的 SELECT SQL；
- 否则通用对话/创作/翻译/写作/编程等不依赖数据库的任务 → intent=chat（explain 放完整回答，输出文字回答即可）；
- 否则恶意请求/违法内容/完全无意义 → intent=refuse。
- 判断为 chat/knowledge/refuse 时，输出正常文字回答即可（回答可自然说明），**禁止**为了凑 SQL 而编造不存在的表或字段。

## 六、系统硬性约束
1. 仅允许 SELECT 查询，禁止 INSERT/UPDATE/DELETE/DDL/多语句、禁止注释注入
2. 只能使用【可用表与字段】中列出的表与字段；**WHERE / GROUP BY / HAVING / ORDER BY 引用的每一个字段，都必须能在【可用表与字段】的对应表名下找到**；Schema 中不存在的字段一律禁止使用（例如某表没有 is_deleted 软删字段时，禁止写 `is_deleted = 0`，也不得想当然补充）；字段类型与注释见 Schema
3. **相对时间必须参数化，禁止固定日期**：用户问「过去N天/近N天/最近N天/昨天/今天/本周/本月」等相对时间时，必须基于数据库当前日期函数（CURDATE()/NOW()）动态推导区间，**禁止**把相对时间写成固定日期字面量（如 `'2026-09-01 00:00:00'`）。参考写法（MySQL，起点取当天 0 点、终点取截止日次日 0 点，左闭右开）：
   - **通用公式：过去N天/近N天=含今天在内的最近N个自然日** → `时间列 >= DATE_SUB(CURDATE(), INTERVAL (N-1) DAY) AND 时间列 < DATE_ADD(CURDATE(), INTERVAL 1 DAY)`；即过去7天 → `时间列 >= DATE_SUB(CURDATE(), INTERVAL 6 DAY) AND 时间列 < DATE_ADD(CURDATE(), INTERVAL 1 DAY)`（终点必须是明天 0 点，**禁止**写成 `< CURDATE()`，否则漏掉今天全天数据）
   - 近30天：`时间列 >= DATE_SUB(CURDATE(), INTERVAL 29 DAY) AND 时间列 < DATE_ADD(CURDATE(), INTERVAL 1 DAY)`
   - 昨天：`时间列 >= DATE_SUB(CURDATE(), INTERVAL 1 DAY) AND 时间列 < CURDATE()`
   - 今天：`时间列 >= CURDATE() AND 时间列 < DATE_ADD(CURDATE(), INTERVAL 1 DAY)`
   - 本月：`时间列 >= DATE_FORMAT(CURDATE(), '%Y-%m-01') AND 时间列 < DATE_ADD(DATE_FORMAT(CURDATE(), '%Y-%m-01'), INTERVAL 1 MONTH)`
   仅当用户给出**明确具体日期**（如「9月1日到9月7日」「2026年8月」）时才使用用户指定的日期字面量
4. 【相似示例】与【知识库参考】仅作口径参考，不得虚构不存在的表字段
5. 若生成 SQL 涉及被权限限制的字段，忽略之（权限由系统自动注入）
6. 若用户问题可对应多张语义相近的表（例如功能测试用例 test_cases 与接口测试用例 api_test_cases 都可能被问到「测试用例」），优先选择与问题最相关的一张表生成 SQL；若确实无法确定，选择注释最匹配的表，不要输出 clarify
7. **软删数据默认排除**：查询涉及的每张表只要存在 `is_deleted` 字段，就必须为其所属表追加 `is_deleted = 0` 条件（如 `tp.is_deleted = 0`，多表多个表级条件用 AND 连接），保证默认只统计未删除的数据；**仅当**用户明确要求查已删除/软删/回收站/全部（含删除）数据时才不加该条件
8. 对名称、标题、项目名、用户名等模糊匹配条件，必须使用 LIKE '%关键词%'（如 WHERE name LIKE '%RT%'），禁止使用 = 精确匹配；仅当用户明确要求精确匹配（如「名称等于XX」「XX 精确」）时才用 =
9. 多轮对话：必须结合【历史对话】理解用户当前问题。若当前消息是澄清确认（如「已确认查询表：xxx」），必须从历史对话中提取用户原始需求（项目名、统计维度、过滤条件等），结合已选表生成 SQL，不得因当前消息简短而输出 refuse
10. 数据分析规则（核心）：根据用户问题意图选择合适的 SQL 形态——
    - 问「多少/数量/统计/计数」→ COUNT(*)，需分组时加 GROUP BY；去重计数用 COUNT(DISTINCT 列)
    - 问「总和/总计/累计」→ SUM(数值列)；问「平均/均值」→ AVG(数值列)
    - 问「趋势/变化/按月/按日/时间分布」→ 用 DATE_FORMAT(时间列,'%Y-%m') 或 DATE(时间列) 作分组维度，加 ORDER BY 时间 ASC
    - 问「排名/前N/最多/最少/TOP」→ ORDER BY 数值列 DESC + LIMIT N
    - 问「占比/比例/构成/百分比」→ 用 分子列/SUM(分子列) OVER() * 100 或子查询计算占比，结果保留 2 位小数
    - 问「对比/比较/分别/各个」→ 按维度 GROUP BY，多维度时用多列分组
    - 问「最新/最近」→ ORDER BY 时间列 DESC + LIMIT 1
11. 结果列必须有业务含义：禁止 SELECT *（除非用户明确要全部字段）；聚合查询只返回维度列 + 指标列；查询字段较多时 LIMIT 100
12. 过滤条件优先走索引：时间范围过滤用 `时间列 >= 起点 AND 时间列 < 终点`（左闭右开，可走索引），**禁止**用 `BETWEEN ... AND ...` 表示时间范围（闭区间含端点，与左闭右开口径冲突）；相对时间的起点/终点用 CURDATE()/NOW() + DATE_SUB/DATE_ADD 推导，日期函数放在常量侧，不要在时间列上套函数；状态过滤用 状态列 = 值
13. **时间过滤必须用左闭右开区间**：查询"今天"用 `时间列 >= CURDATE() AND 时间列 < DATE_ADD(CURDATE(), INTERVAL 1 DAY)`；查询"昨天"用 `时间列 >= DATE_SUB(CURDATE(), INTERVAL 1 DAY) AND 时间列 < CURDATE()`；查询指定日期"9月4日"用 `时间列 >= '2026-09-04 00:00:00' AND 时间列 < '2026-09-05 00:00:00'`；**禁止** `BETWEEN '2026-09-04' AND '2026-09-04'`（同日闭区间两端都是 0 点，会漏掉全天数据）；禁止 `= '2026-09-04'`（只匹配 0 点整）
14. **子查询过滤必须用 IN**：按名称模糊匹配项目/实体再取其 ID 过滤时，禁止 `x = (SELECT id FROM ... WHERE name LIKE ...)`（可能返回多行报 1242），必须写 `x IN (SELECT id FROM ... WHERE name LIKE ...)`
15. **禁止对 ID/外键类字段做聚合**：id、*_id 结尾字段（主键/外键，如 project_id、req_id、api_id）只用于关联、过滤、分组，**禁止** SUM/AVG/MAX/MIN(project_id) 这类无意义聚合；聚合函数只允许作用于数值业务指标（金额/数量/时长/次数/比率/大小等）。「按X项目」「查X项目/项目下的Y」是维度筛选（WHERE 项目名 LIKE + GROUP BY 项目名/名称列），不是对项目ID求和"""


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
        return "\n".join(lines)
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


# 选表 LLM 的 system prompt（V1.1：全表清单 + 选表规则拼进 system prompt）
SELECT_TABLE_SYSTEM = """你是严格的数据库表选择器。任务：根据用户的数据查询问题，从【数据表清单】中选出本次查询需要的全部数据表。
规则：
1. 依据表名、表中文注释，以及【用户查询目标】/【文件大小类字段命中】块判断相关性：命中用户目标字段的表优先（如 size 文件大小 + upload_status 上传状态）；表注释为空时以字段命中为准，禁止仅凭表名猜测；
2. 需要关联查询时，主表与关联链路涉及的表都必须选出（如按项目名称过滤→选出项目表；统计接口调用次数→选出执行记录表及其关联对象表）；
3. 只选必需的表，无关表一律不选；确实无任何相关表时输出空数组 []；
4. 必须结合【历史对话】理解省略式/指代式追问（如“环比去年呢”“那每个接口呢”），沿用上一轮已确认的表；
5. 只输出一个 JSON 数组，元素结构为 {"id": 表ID, "table_name": "表名", "comment": "表注释"}，id/table_name 必须逐字来自清单，禁止编造；除 JSON 数组外无任何多余文字。"""


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
        _hints = ((spec_context or {}).get("spec") or {}).get("table_hints") or []
        _spec_d = (spec_context or {}).get("spec") or {}
        _target = []
        for _m in (_spec_d.get("metrics") or []) + (_spec_d.get("dimensions") or []):
            _n = _m.get("name")
            if _n and _n not in _target:
                _target.append(_n)
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
        _mapping = (spec_context or {}).get("mapping") or {}
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
- 判断为 query 时，必须在 ```sql 代码块内输出完整可执行的 SELECT SQL；
- 判断为 chat/knowledge/refuse 时，输出正常文字回答即可（回答可自然说明），**禁止**为了凑 SQL 而编造不存在的表或字段。

【用户问题】{question}

严格按系统提示词「四、输出格式」输出：**仅输出** ```sql 代码块（含必要注释），无任何多余文字、解释、说明。"""
    if exec_error:
        user_prompt += f"""
【上次执行失败，必须修正】
{exec_error}
请分析错误原因并修正 SQL：**保持用户问题的查询语义不变**（项目/时间/状态等过滤条件、分组维度、计数/聚合形态都不得删减或改变），只修正报错本身；只使用【可用表与字段】中确切存在的表名和字段；聚合查询中 GROUP BY 必须包含 SELECT 中全部非聚合列（注意 only_full_group_by 模式）；若错误为 Subquery returns more than 1 row，必须把 `= (SELECT ...)` 改为 `IN (SELECT ...)`；若错误为 Column 'xxx' in field list is ambiguous（列名歧义），必须为 SELECT、ORDER BY、WHERE、GROUP BY 中的重名列显式加上表别名限定（如 stat_a.xxx、stat_b.xxx），并保证 JOIN 条件与 SELECT 列使用同一别名；修正后**仅输出**修正后的 ```sql 代码块（含必要注释）。"""

    client = llm
    if client is None or not client.configured:
        yield {"type": "result", "result": _mock_sql(datasource_id, question, dialect)}
        return

    from .sql_extractor import extract_first_sql
    from ..executor import validate_sql
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}]
    from ..config import get_settings
    attempts = (retries if retries is not None else get_settings().sql_correct_retries)
    last_error = ""

    for i in range(attempts + 1):
        # 流式调用 LLM（自然语言 + markdown SQL；关闭内部 thinking 避免 Qwen3.5-4B 思考链挤占 content）
        content = ""
        force_retry = False
        stream_gen = client.chat_stream(messages, json_mode=False, max_tokens=4096, thinking=False)
        while True:
            try:
                chunk = next(stream_gen)
                c_delta = chunk.get("content", "")
                r_delta = chunk.get("reasoning", "")
                content += c_delta
                if r_delta:
                    yield {"type": "thinking", "delta": r_delta}
                if c_delta:
                    yield {"type": "stream", "delta": c_delta}
                    # 提前中断1：检测到完整的 ```sql ... ``` 代码块就停止
                    if "```sql" in content and content.rfind("```") > content.find("```sql") + 5:
                        break
                    # 提前中断2：思考内容超过 1500 字仍无 SQL，强制中断重试（推理模型思考链过长）
                    if len(content) > 1500 and "```sql" not in content:
                        last_error = "模型思考过程过长，已强制中断"
                        yield {"type": "retry", "msg": "正在精简输出…"}
                        messages.append({"role": "assistant", "content": content[:500]})
                        messages.append({"role": "user",
                                         "content": "【重要】不要展开分析过程，不要编号列举，严格按系统提示词「四、输出格式」：**仅输出** ```sql 代码块（含必要注释）。"})
                        force_retry = True
                        break
            except StopIteration as e:
                val = e.value or {}
                content = (val.get("content") or content).strip()
                break
        if force_retry:
            continue

        # 从回答中提取 SQL（markdown ```sql 代码块优先，兜底裸 SELECT）
        sql = extract_first_sql(content) or ""
        # 排查辅助：每次 LLM 响应全文打印到日志（含提取结果）
        logger.info("[NL2SQL] 第 %d 次尝试, 提取=%s\n-----响应原文-----\n%s\n-----END-----",
                    i + 1, "OK" if sql else "未提取到SQL", content)
        if not sql:
            non_sql = _detect_non_sql_answer(content)
            if non_sql:
                # 模型判定非数据查询（chat/knowledge/refuse）：直接返回文字回答，不重试
                logger.info("[NL2SQL] 判定为 %s 回答，跳过 SQL 重试", non_sql)
                yield {"type": "result", "result": {
                    "intent": non_sql, "sql": "", "explain": content,
                    "tables": [t.table_name for t in tables],
                    "selected_tables": select_meta.get("raw", []),
                    "select_source": select_meta.get("source", ""),
                    "rag_hits": _rag_hits}}
                return
            last_error = "未从回答中提取到 SELECT SQL 代码块"
            yield {"type": "retry", "msg": "未识别到 SQL，正在重试…"}
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user",
                             "content": "【重要】上一次回答中没有找到可执行的 SELECT SQL。请严格按系统提示词「四、输出格式」：**仅输出** 完整闭合的 ```sql 代码块（含必要注释），无任何多余文字。"})
            continue

        # 校验 SQL（语法/只读/表存在/字段存在/计数形状），通过则应用中文字段别名
        logger.info("[NL2SQL] 提取后SQL前300字: %s", sql[:300])
        try:
            validate_sql(sql, dialect)
            _check_tables_exist(sql, datasource_id)
            _check_columns_exist(sql, datasource_id)
            shape_err = _count_shape_error(sql, spec_context)
            if shape_err:
                raise ValueError(shape_err)
            sql = _apply_column_aliases(sql, datasource_id)
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
            yield {"type": "retry", "msg": "SQL 校验中，正在修正…"}
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user",
                             "content": f"SQL 校验失败：{last_error}。请仅修正报错内容，**保持用户问题的查询语义不变**"
                                         "（项目/时间/状态等过滤条件、分组维度、计数/聚合形态都不得删减或改变）。"
                                         "修正后用 ```sql 代码块重新输出完整的 SELECT SQL；SQL 只能使用【可用表与字段】中列出的确切表名和字段名。"})

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
                        exec_error: str | None = None):
    """流式生成 SQL；yield 事件：
    - {"type": "stream", "delta": "..."}  LLM 输出逐块（打字机效果）
    - {"type": "retry", "msg": "..."}     校验失败正在重试
    - {"type": "result", "result": {...}}  最终结果（与 generate_sql 返回结构一致）
    逻辑与 generate_sql 完全一致，仅 LLM 调用改为流式。
    spec_context：AI 问数重构后由意图层传入 {"spec": {...}, "mapping": {...}, "plan": {...}}，
    LLM 退化为"翻译器"，只能按结构化参数生成 SQL（模板无法覆盖时的兜底路径）；
    detail 意图且未指定指标/字段时放宽为"从可用表与字段中按问题检索展示字段"。
    schema_name：查询范围 schema（项目/库），未显式传入时取 spec_context.spec.schema。"""
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
        _hints = ((spec_context or {}).get("spec") or {}).get("table_hints") or []
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
- 判断为 query 时，必须在 ```sql 代码块内输出完整可执行的 SELECT SQL；
- 判断为 chat/knowledge/refuse 时，输出正常文字回答即可（回答可自然说明），**禁止**为了凑 SQL 而编造不存在的表或字段。

【用户问题】{question}

严格按系统提示词「四、输出格式」输出：**仅输出** ```sql 代码块（含必要注释），无任何多余文字、解释、说明。"""
    if exec_error:
        user_prompt += f"""
【上次执行失败，必须修正】
{exec_error}
请分析错误原因并修正 SQL：**保持用户问题的查询语义不变**（项目/时间/状态等过滤条件、分组维度、计数/聚合形态都不得删减或改变），只修正报错本身；只使用【可用表与字段】中确切存在的表名和字段；聚合查询中 GROUP BY 必须包含 SELECT 中全部非聚合列（注意 only_full_group_by 模式）；若错误为 Subquery returns more than 1 row，必须把 `= (SELECT ...)` 改为 `IN (SELECT ...)`；若错误为 Column 'xxx' in field list is ambiguous（列名歧义），必须为 SELECT、ORDER BY、WHERE、GROUP BY 中的重名列显式加上表别名限定（如 stat_a.xxx、stat_b.xxx），并保证 JOIN 条件与 SELECT 列使用同一别名；修正后**仅输出**修正后的 ```sql 代码块（含必要注释）。"""

    client = llm
    if client is None or not client.configured:
        yield {"type": "result", "result": _mock_sql(datasource_id, question, dialect)}
        return

    from .sql_extractor import extract_first_sql
    from ..executor import validate_sql
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}]
    from ..config import get_settings
    attempts = (retries if retries is not None else get_settings().sql_correct_retries)
    last_error = ""

    for i in range(attempts + 1):
        # 流式调用 LLM（自然语言 + markdown SQL；关闭内部 thinking 避免 Qwen3.5-4B 思考链挤占 content）
        content = ""
        force_retry = False
        stream_gen = client.chat_stream(messages, json_mode=False, max_tokens=4096, thinking=False)
        while True:
            try:
                chunk = next(stream_gen)
                c_delta = chunk.get("content", "")
                r_delta = chunk.get("reasoning", "")
                content += c_delta
                if r_delta:
                    yield {"type": "thinking", "delta": r_delta}
                if c_delta:
                    yield {"type": "stream", "delta": c_delta}
                    # 提前中断1：检测到完整的 ```sql ... ``` 代码块就停止
                    if "```sql" in content and content.rfind("```") > content.find("```sql") + 5:
                        break
                    # 提前中断2：思考内容超过 1500 字仍无 SQL，强制中断重试（推理模型思考链过长）
                    if len(content) > 1500 and "```sql" not in content:
                        last_error = "模型思考过程过长，已强制中断"
                        yield {"type": "retry", "msg": "正在精简输出…"}
                        messages.append({"role": "assistant", "content": content[:500]})
                        messages.append({"role": "user",
                                         "content": "【重要】不要展开分析过程，不要编号列举，严格按系统提示词「四、输出格式」：**仅输出** ```sql 代码块（含必要注释）。"})
                        force_retry = True
                        break
            except StopIteration as e:
                val = e.value or {}
                content = (val.get("content") or content).strip()
                break
        if force_retry:
            continue

        # 从回答中提取 SQL（markdown ```sql 代码块优先，兜底裸 SELECT）
        sql = extract_first_sql(content) or ""
        # 排查辅助：每次 LLM 响应全文打印到日志（含提取结果）
        logger.info("[NL2SQL] 第 %d 次尝试, 提取=%s\n-----响应原文-----\n%s\n-----END-----",
                    i + 1, "OK" if sql else "未提取到SQL", content)
        if not sql:
            non_sql = _detect_non_sql_answer(content)
            if non_sql:
                # 模型判定非数据查询（chat/knowledge/refuse）：直接返回文字回答，不重试
                logger.info("[NL2SQL] 判定为 %s 回答，跳过 SQL 重试", non_sql)
                yield {"type": "result", "result": {
                    "intent": non_sql, "sql": "", "explain": content,
                    "tables": [t.table_name for t in tables],
                    "selected_tables": select_meta.get("raw", []),
                    "select_source": select_meta.get("source", ""),
                    "rag_hits": _rag_hits}}
                return
            last_error = "未从回答中提取到 SELECT SQL 代码块"
            yield {"type": "retry", "msg": "未识别到 SQL，正在重试…"}
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user",
                             "content": "【重要】上一次回答中没有找到可执行的 SELECT SQL。请严格按系统提示词「四、输出格式」：**仅输出** 完整闭合的 ```sql 代码块（含必要注释），无任何多余文字。"})
            continue

        # 校验 SQL（语法/只读/表存在/字段存在/计数形状），通过则应用中文字段别名
        logger.info("[NL2SQL] 提取后SQL前300字: %s", sql[:300])
        try:
            validate_sql(sql, dialect)
            _check_tables_exist(sql, datasource_id)
            _check_columns_exist(sql, datasource_id)
            shape_err = _count_shape_error(sql, spec_context)
            if shape_err:
                raise ValueError(shape_err)
            sql = _apply_column_aliases(sql, datasource_id)
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
            yield {"type": "retry", "msg": "SQL 校验中，正在修正…"}
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user",
                             "content": f"SQL 校验失败：{last_error}。请仅修正报错内容，**保持用户问题的查询语义不变**"
                                         "（项目/时间/状态等过滤条件、分组维度、计数/聚合形态都不得删减或改变）。"
                                         "修正后用 ```sql 代码块重新输出完整的 SELECT SQL；SQL 只能使用【可用表与字段】中列出的确切表名和字段名。"})

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
        # 列出每个引用表的可用列名，帮助 LLM 一次修正（避免把注释当列名）
        hints = []
        for rt in sorted(ref_tables):
            if rt in cols_of:
                available = ", ".join(sorted(cols_of[rt]))
                hints.append(f"{rt} 可用列: {available}")
        hint_str = ("；" + "；".join(hints)) if hints else ""
        raise SqlExecError(
            "字段不存在：" + ", ".join(sorted(set(unknown)))
            + hint_str
            + "；请使用上表中的确切字段名修正 SQL")
