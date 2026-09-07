"""从大模型回答文本中提取 SQL 语句（参考 xp-eoitool-python 实现）。

提取规则（优先级从高到低）：
1. ```sql ... ``` 代码块
2. ``` ... ``` 通用代码块（内容以 SELECT/WITH 开头）
3. 裸 SELECT / WITH 语句（兜底）

安全约束：只返回 SELECT / WITH...SELECT 语句，写操作不提取。
"""
import re

_ALLOWED_PREFIXES = re.compile(r'^\s*(SELECT|WITH)\b', re.IGNORECASE)
_FORBIDDEN = re.compile(
    r'^\s*(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|REPLACE|CALL|EXEC)\b',
    re.IGNORECASE,
)


def _is_safe_select(sql: str) -> bool:
    sql = sql.strip()
    if _FORBIDDEN.match(sql):
        return False
    return bool(_ALLOWED_PREFIXES.match(sql))


def _clean(sql: str) -> str:
    sql = sql.strip().rstrip(';').strip()
    # 去除可能残留的代码块标记（提取器边界情况）
    sql = sql.strip('`').strip()
    if sql.lower().startswith("sql"):
        sql = sql[3:].strip()
    return sql


def extract_sqls(answer: str) -> list[str]:
    """从回答文本中提取所有 SELECT SQL（去重保序）。"""
    if not answer:
        return []
    results = []
    seen = set()

    def _add(sql: str):
        sql = _clean(sql)
        if sql and _is_safe_select(sql) and sql not in seen:
            seen.add(sql)
            results.append(sql)

    # 优先级 1：```sql ... ```（容忍未闭合的代码块：取到文本末尾）
    for m in re.finditer(r'```sql\s*(.*?)(?:```|\Z)', answer, re.DOTALL | re.IGNORECASE):
        cand = m.group(1).strip()
        if cand and not cand.lower().startswith("select") and "select " not in cand[:60].lower():
            continue  # 未闭合且内容明显不是 SQL（可能是解释文字）时跳过
        _add(cand)

    # 优先级 1.5：```sql 已出现但整体被截断（从 ```sql 之后到文本末尾，去掉尾部残缺词）
    if not results:
        idx = answer.lower().find("```sql")
        if idx != -1:
            tail = answer[idx + 6:].strip()
            _add(tail)

    # 优先级 2：通用 ``` ... ```（内容以 SELECT/WITH 开头，容忍未闭合）
    if not results:
        for m in re.finditer(r'```\s*(.*?)(?:```|\Z)', answer, re.DOTALL):
            candidate = m.group(1).strip()
            if _ALLOWED_PREFIXES.match(candidate):
                _add(candidate)

    # 优先级 3：JSON 包裹 {"sql": "SELECT ..."} / SQL: SELECT ...（模型偶发输出）
    if not results:
        m = re.search(r'["\'\']sql["\'\']\s*[:：]\s*["\']((?:SELECT|WITH)[^"\'\']*)["\']',
                      answer, re.IGNORECASE)
        if m:
            _add(m.group(1))

    # 优先级 4：裸 SELECT 语句（容忍行首 SQL:/SQL：前缀）
    if not results:
        for m in re.finditer(
            r'(?:^|\n)[^\S\n]*(?:SQL\s*[:：]\s*)?((?:SELECT|WITH)\s.+?)(?:;|\Z)',
            answer, re.IGNORECASE | re.DOTALL,
        ):
            _add(m.group(1))

    return results


def extract_first_sql(answer: str) -> str | None:
    """提取第一条 SQL，没有则返回 None。"""
    sqls = extract_sqls(answer)
    return sqls[0] if sqls else None
