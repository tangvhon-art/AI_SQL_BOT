"""L-0 公共化地基层：SYSTEM_PROMPT 拼装式片段复用。

从 nl2sql.SYSTEM_PROMPT / intent.SPEC_EXTRACT_PROMPT / doc_chat.SYSTEM_PROMPT
等各模块各自抄写的 prompt 片段中抽离为可复用的拼装函数。

各模块的 prompt 只做拼装：
    SYSTEM_PROMPT = "\\n".join([
        prompt_kit.safety_guardrails(),
        "你是数据库表选择器...",
    ])
"""
from __future__ import annotations

from typing import Any


def safety_guardrails() -> str:
    """SQL 安全硬约束段。

    从 nl2sql.SYSTEM_PROMPT 第 106-128 行抽离。
    包含：仅 SELECT / 禁 DDL / 字段必须存在于 Schema / 软删除 / 左闭右开 / 子查询 IN /
    ID 不聚合 / 模糊匹配用 LIKE。
    """
    return """## 系统硬性约束
1. 仅允许 SELECT 查询，禁止 INSERT/UPDATE/DELETE/DDL/多语句、禁止注释注入
2. 只能使用【可用表与字段】中列出的表与字段；WHERE / GROUP BY / HAVING / ORDER BY 引用的每一个字段，都必须能在【可用表与字段】的对应表名下找到；Schema 中不存在的字段一律禁止使用（例如某表没有 is_deleted 软删字段时，禁止写 `is_deleted = 0`，也不得想当然补充）；字段类型与注释见 Schema
3. **相对时间必须参数化，禁止固定日期**：用户问「过去N天/近N天/最近N天/昨天/今天/本周/本月」等相对时间时，必须基于数据库当前日期函数（CURDATE()/NOW()）动态推导区间，**禁止**把相对时间写成固定日期字面量（如 `'2026-09-01 00:00:00'`）。参考写法（MySQL，起点取当天 0 点、终点取截止日次日 0 点，左闭右开）：过去N天/近N天=含今天在内的最近N个自然日 → `时间列 >= DATE_SUB(CURDATE(), INTERVAL (N-1) DAY) AND 时间列 < DATE_ADD(CURDATE(), INTERVAL 1 DAY)`（过去7天 → INTERVAL 6 DAY，终点必须是明天 0 点，禁止写成 `< CURDATE()` 否则漏掉今天）；近30天 → `时间列 >= DATE_SUB(CURDATE(), INTERVAL 29 DAY) AND 时间列 < DATE_ADD(CURDATE(), INTERVAL 1 DAY)`；昨天 → `时间列 >= DATE_SUB(CURDATE(), INTERVAL 1 DAY) AND 时间列 < CURDATE()`；今天 → `时间列 >= CURDATE() AND 时间列 < DATE_ADD(CURDATE(), INTERVAL 1 DAY)`；本月 → `时间列 >= DATE_FORMAT(CURDATE(), '%Y-%m-01') AND 时间列 < DATE_ADD(DATE_FORMAT(CURDATE(), '%Y-%m-01'), INTERVAL 1 MONTH)`。仅当用户给出明确具体日期（如「9月1日到9月7日」「2026年8月」）时才使用用户指定的日期字面量
4. 【相似示例】与【知识库参考】仅作口径参考，不得虚构不存在的表字段
5. 若生成 SQL 涉及被权限限制的字段，忽略之（权限由系统自动注入）
6. **软删数据默认排除**：查询涉及的每张表只要存在 `is_deleted` 字段，就必须为其所属表追加 `is_deleted = 0` 条件（如 `tp.is_deleted = 0`，多表多个表级条件用 AND 连接），保证默认只统计未删除的数据；**仅当**用户明确要求查已删除/软删/回收站/全部（含删除）数据时才不加该条件
7. 对名称、标题、项目名、用户名等模糊匹配条件，必须使用 LIKE '%关键词%'（如 WHERE name LIKE '%RT%'），禁止使用 = 精确匹配；仅当用户明确要求精确匹配（如「名称等于XX」「XX 精确」）时才用 =
8. **时间过滤必须用左闭右开区间**：查询"今天"用 `时间列 >= CURDATE() AND 时间列 < DATE_ADD(CURDATE(), INTERVAL 1 DAY)`；查询"昨天"用 `时间列 >= DATE_SUB(CURDATE(), INTERVAL 1 DAY) AND 时间列 < CURDATE()`；查询指定日期"9月4日"用 `时间列 >= '2026-09-04 00:00:00' AND 时间列 < '2026-09-05 00:00:00'`；**禁止** `BETWEEN '2026-09-04' AND '2026-09-04'`（同日闭区间两端都是 0 点，会漏掉全天数据）；禁止 `= '2026-09-04'`（只匹配 0 点整）
9. **子查询过滤必须用 IN**：按名称模糊匹配项目/实体再取其 ID 过滤时，禁止 `x = (SELECT id FROM ... WHERE name LIKE ...)`（可能返回多行报 1242），必须写 `x IN (SELECT id FROM ... WHERE name LIKE ...)`
10. **禁止对 ID/外键类字段做聚合**：id、*_id 结尾字段（主键/外键，如 project_id、req_id、api_id）只用于关联、过滤、分组，**禁止** SUM/AVG/MAX/MIN(project_id) 这类无意义聚合；聚合函数只允许作用于数值业务指标（金额/数量/时长/次数/比率/大小等）"""


def schema_block(tables: list[Any] | None = None,
                 cols_by_table: dict[int, list] | None = None,
                 preferred: set[str] | None = None) -> str:
    """Schema 格式化段：`表名: 列(类型·注释), ...`。

    从 nl2sql._schema_text 抽离。
    tables 为 TableMeta 列表，cols_by_table 为 {table_id: [ColumnMeta, ...]}。
    preferred 字段加 ★ 前缀。
    """
    if not tables:
        return "（无）"
    preferred = preferred or set()
    cols_by_table = cols_by_table or {}
    lines = []
    for t in tables:
        parts = []
        for c in cols_by_table.get(t.id, []):
            seg = c.column_name
            type_or_comment = "·".join(x for x in (c.data_type, c.comment) if x)
            if type_or_comment:
                seg += f"({type_or_comment})"
            if c.column_name in preferred:
                seg = f"★{seg}"
            parts.append(seg)
        lines.append(f"{t.table_name}: {', '.join(parts)}")
    return "\n".join(lines)


def relationship_block(relations: list[dict]) -> str:
    """表关系段：`左表.列 → 右表.列（rel_type/source）`。

    从 nl2sql._join_paths_text 抽离。
    relations 为 [{src_table, src_col, dst_table, dst_col, rel_type, source}, ...]。
    """
    if not relations:
        return "（无）"
    out = []
    for r in relations:
        rel = f"（{r.get('rel_type', '')}/{r.get('source', '')}）"
        if not r.get("rel_type") and not r.get("source"):
            rel = ""
        out.append(f"{r['src_table']}.{r['src_col']} → {r['dst_table']}.{r['dst_col']}{rel}")
    return "; ".join(out)


def history_block(history: list[dict]) -> str:
    """历史对话段（含上轮 SQL / confirmed_tables）。

    从 nl2sql 上下文拼接段抽离。
    """
    if not history:
        return "（无）"
    lines = []
    for m in history[-6:]:
        role_label = "用户" if m.get("role") == "user" else "助手"
        line = f"{role_label}: {m.get('text', '')}"
        prev_sql = m.get("sql") or ""
        if prev_sql:
            line += f"\n  [上一次SQL] {prev_sql}"
        lines.append(line)
    return "\n".join(lines)


def few_shot_block(examples: list[dict]) -> str:
    """few-shot 示例段（问题→SQL）。

    从 nl2sql.retrieve_few_shots 拼接段抽离。
    examples 为 [{question, sql}, ...]。
    """
    if not examples:
        return "（暂无示例）"
    return "\n".join(f"问题：{e['question']}\nSQL：{e['sql']}" for e in examples)


def rag_block(rag_faq: list[dict], rag_doc: list[dict]) -> tuple[str, str]:
    """RAG 检索结果拼装段。

    返回 (faq_text, doc_text)。
    从 nl2sql generate_sql_stream 中的 rag_faq_text/rag_doc_text 抽离。
    """
    faq_text = "\n".join(
        f"- Q：{h['content'].split(chr(10))[0][3:]}\n  A：{chr(10).join(h['content'].split(chr(10))[1:]).lstrip('A：')}"
        if h.get('content') else ""
        for h in rag_faq
    ) or "（无命中）"
    doc_text = "\n".join(f"- {h['content'][:400]}" for h in rag_doc) or "（无命中）"
    return faq_text, doc_text


def confirmed_section_block(tables: list[Any]) -> str:
    """用户已确认表段（多轮澄清点选）。

    从 nl2sql.confirmed_section 抽离。
    """
    if not tables or len(tables) < 2:
        return ""
    confirmed_names = "、".join(t.table_name for t in tables)
    return f"""
【用户已确认选择的表（多轮澄清中明确勾选，共{len(tables)}张）】
{confirmed_names}
生成 SQL 时必须综合考虑以上所有确认表，不得遗漏任何一张（除非该表与问题完全无关，须在注释中说明原因）。
请结合表注释与关系选择 JOIN / UNION ALL / 明细形态。"""


def base_constraints() -> str:
    """基础约束段（核心指令 + 表关联规范 + 语法编写规范 + 输出格式）。

    从 nl2sql.SYSTEM_PROMPT 第 52-98 行抽离。
    """
    return """## 核心指令
请根据用户的**业务查询问题**，结合提供的【可用表与字段】（完整数据库表结构：表名/字段名/字段类型/注释）、【JOIN 路径】（表关联关系：主键/外键），严格匹配需求，生成**可直接运行、语法标准、逻辑严谨**的 SQL 语句。
**SQL 必须与用户问题语义严格一致**：用户询问数量/计数（如"…有多少/几个/几条/分别多少"）时，必须输出聚合查询（COUNT/SUM）并按分组字段 GROUP BY，**禁止**把计数问题写成 `SELECT ... LIMIT n` 的明细行查询；用户明确要明细/列表时才返回明细字段。

## 基础约束（强制）
1. 无表/无对应字段时：**仅输出**「未检索到相关表/字段，无法生成SQL语句」，不做任何假设；
2. 生成 SQL 时，**所有输出仅包含 SQL 代码 + 关键注释**，无任何多余文字、解释、说明；
3. 查询结果字段**必须全部转换为中文别名**（每一个 SELECT 列都要 `AS 中文别名`）；
4. **ID 字段治理（强制）**：结果中**禁止直接展示纯标识类 ID 字段**，凡业务上有对应名称表的，必须通过 JOIN 关联取名称列展示；
5. SQL 格式：**Markdown 代码块**包裹，语言标记为 `sql`。

## 表关联规范（强制）
1. 多表查询**必须使用标准 JOIN...ON**，**禁止**使用逗号分隔表的旧式关联写法；
2. 关联条件**严格遵循【JOIN 路径】提供的主键-外键关联**，无自定义、无错误关联。

## 语法编写规范（强制）
1. 复杂查询**优先使用 CTE(WITH子句)** 拆分业务逻辑；
2. 支持：子查询、`GROUP BY`/`HAVING`、`ORDER BY`、`LIMIT`、`WHERE`、`DISTINCT`、聚合函数；
3. 严格贴合需求：**不新增无关条件、不返回多余字段、不修改业务逻辑**。"""
