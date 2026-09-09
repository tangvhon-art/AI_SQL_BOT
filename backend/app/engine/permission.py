"""字段级权限引擎：规则合并（可查并集/不可查并集、黑名单优先）与 SQL 改写（1=1 / 1=2）。

口径（需求 V1.3，原文约束）：
- 权限由数据库表 permission_rule 控制（scope=role 角色全局 / scope=user 用户级）
- 可查权限 = 角色可查 ∪ 用户可查；不可查权限 = 角色不可查 ∪ 用户不可查
- 不可查优先：不可查包含可查时以不可查为准（allow = allow - deny）
- 查询命中不可查字段 → 注入 AND 1=2；全部可查 → 注入 AND 1=1（保证后续改写可叠加且无副作用）
"""
import logging

import sqlglot
import sqlglot.expressions as exp

from ..database import SessionLocal

logger = logging.getLogger(__name__)


class PermissionDenied(Exception):
    pass


def compute_permissions(user_id: int, workspace_id: int) -> tuple[set[str], set[str]]:
    """返回 (allow_keys, deny_keys)，key 形如 "{datasource_id}.{schema}.{table}.{column}"。
    column_id 为 NULL 的规则视为整表规则，展开为该表全部列。
    生效范围：用户直接角色 ∪ 用户所属用户组的角色 ∪ 用户/用户组直接规则（取并集）。"""
    from ..models import (ColumnMeta, PermissionRule, RoleUser,
                          RoleUserGroup, TableMeta, UserGroupMember)

    db = SessionLocal()
    try:
        user = db.query(sqlalchemy_user_model()).get(user_id)
        # 用户生效角色：user.role_id + role_user + 用户组角色(role_user_group)
        role_ids: set[int] = set()
        group_ids: set[int] = set()
        if user:
            if user.role_id:
                role_ids.add(user.role_id)
            for ru in db.query(RoleUser).filter(RoleUser.user_id == user_id).all():
                role_ids.add(ru.role_id)
            group_ids = {m.group_id for m in db.query(UserGroupMember)
                         .filter(UserGroupMember.user_id == user_id).all()}
            if group_ids:
                for rg in (db.query(RoleUserGroup)
                           .filter(RoleUserGroup.group_id.in_(group_ids)).all()):
                    role_ids.add(rg.role_id)
        rules = (db.query(PermissionRule)
                 .filter(PermissionRule.workspace_id == workspace_id,
                         PermissionRule.enabled.is_(True))
                 .all())
        scope_hit = set()
        for r in rules:
            if r.scope_type == "user" and r.scope_id == user_id:
                scope_hit.add((r, "user"))
            if r.scope_type == "role" and r.scope_id in role_ids:
                scope_hit.add((r, "role"))
            if r.scope_type == "group" and r.scope_id in group_ids:
                scope_hit.add((r, "group"))

        allow: set[str] = set()
        deny: set[str] = set()
        for r, _ in scope_hit:
            tm = db.query(TableMeta).get(r.table_id)
            if not tm:
                continue
            cols = (db.query(ColumnMeta)
                    .filter(ColumnMeta.table_meta_id == r.table_id).all())
            col_names = [c.column_name for c in cols]
            # column_ids 空数组或 [0]（整表标记）= 整表规则；非空 = 指定字段列表
            rule_col_ids = [c for c in (r.column_ids or []) if c != 0]
            if not rule_col_ids:
                targets = col_names
            else:
                id_to_name = {c.id: c.column_name for c in cols}
                targets = [id_to_name[cid] for cid in rule_col_ids if cid in id_to_name]
            ds = r.datasource_id
            for col in targets:
                key = f"{ds}.{tm.schema_name}.{tm.table_name}.{col}"
                (allow if r.rule_type == "allow" else deny).add(key)
        # 黑名单优先：可查并集 - 不可查并集
        allow = allow - deny
        return allow, deny
    finally:
        db.close()


def sqlalchemy_user_model():
    from ..models import User
    return User


def _column_key(table_alias: str, col: str, schema: str, table: str, ds_id: int) -> str:
    return f"{ds_id}.{schema}.{table}.{col}"


def extract_referenced_columns(ast: exp.Expression, schema: str, table: str,
                               ds_id: int) -> list[str]:
    """提取 SELECT/WHERE/GROUP BY/ORDER BY/HAVING/JOIN 中引用的列（不含子查询内层递归去重）。"""
    from ..models import ColumnMeta, TableMeta
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        tm = (db.query(TableMeta)
              .filter(TableMeta.datasource_id == ds_id,
                      TableMeta.schema_name == schema,
                      TableMeta.table_name == table)
              .first())
        if not tm:
            return []
        known = {c.column_name for c in
                 db.query(ColumnMeta).filter(ColumnMeta.table_meta_id == tm.id).all()}
    finally:
        db.close()

    # 构建别名→真实表名映射（处理 FROM api_test_cases a 这类别名场景）
    alias_to_table: dict[str, str] = {}
    for t in ast.find_all(exp.Table):
        if t.alias:
            alias_to_table[str(t.alias).lower()] = t.name.lower()
        if t.name:
            alias_to_table[t.name.lower()] = t.name.lower()

    refs: list[str] = []
    for node in ast.find_all(exp.Column):
        name = str(node.name)
        if name.lower() not in {c.lower() for c in known}:
            continue
        if node.table:
            tbl_alias = str(node.table).lower()
            real_table = alias_to_table.get(tbl_alias, tbl_alias)
            if real_table != table.lower():
                continue
        # 无表限定符：列名在目标表已知列中即提取（保守假设属于该表）
        refs.append(name)
    return list(dict.fromkeys(refs))


def _tables_of_query(ast: exp.Expression) -> list[tuple[str, str]]:
    """返回查询涉及的 (schema, table) 列表（含 JOIN）。"""
    result = []
    for table in ast.find_all(exp.Table):
        name = table.name
        schema = table.db or ""
        result.append((schema, name))
    # 去重保序
    seen = set()
    out = []
    for s, t in result:
        key = (s.lower(), t.lower())
        if key not in seen:
            seen.add(key)
            out.append((s, t))
    return out


def _mask_columns_in_projections(ast: exp.Expression,
                                 denied_cols: list[tuple[str, str]]) -> None:
    """遍历所有 SELECT 投影，把引用被限制字段的列替换为 '****'（保留原别名）。

    denied_cols: [(table, column), ...] 表名和字段名均为小写比较。
    处理 SELECT 列表中的 exp.Column / exp.Alias(exp.Column)，聚合函数内的字段不替换
    （如 COUNT(title) 保留，因为结果是数值不是字段值）。
    """
    denied_set = {(t.lower(), c.lower()) for t, c in denied_cols}

    # 构建别名→真实表名映射（处理 FROM test_cases AS tc 这类别名场景）
    alias_to_table: dict[str, str] = {}
    for t in ast.find_all(exp.Table):
        if t.alias:
            alias_to_table[str(t.alias).lower()] = t.name.lower()
        if t.name:
            alias_to_table[t.name.lower()] = t.name.lower()

    def _col_is_denied(col: exp.Column) -> bool:
        col_name = str(col.name).lower()
        # 无表限定符时，只要字段名匹配任一被限制字段即视为命中（保守）
        if not col.table:
            return any(c == col_name for _, c in denied_set)
        tbl_alias = str(col.table).lower()
        # 别名映射到真实表名
        real_table = alias_to_table.get(tbl_alias, tbl_alias)
        return (real_table, col_name) in denied_set

    def _replace_projection(proj: exp.Expression) -> exp.Expression:
        """返回替换后的投影节点；未命中则返回原节点。"""
        alias = None
        inner = proj
        if isinstance(proj, exp.Alias):
            alias = proj.alias
            inner = proj.this
        # 只替换直接的字段引用（不含聚合/函数嵌套）
        if isinstance(inner, exp.Column) and _col_is_denied(inner):
            literal = exp.Literal.string("****")
            if alias:
                return exp.Alias(this=literal, alias=alias)
            # 无别名时用原列名作为别名，保证 CTE/子查询外层引用 tc.title 时可命中
            return exp.Alias(this=literal, alias=str(inner.name))
        return proj

    for select in ast.find_all(exp.Select):
        new_exprs = []
        changed = False
        for proj in select.expressions:
            new_proj = _replace_projection(proj)
            if new_proj is not proj:
                changed = True
            new_exprs.append(new_proj)
        if changed:
            select.set("expressions", new_exprs)


def rewrite_sql_for_user(ast: exp.Expression, dialect: str, user_id: int) -> tuple[str, str, list[str], str]:
    """注入 1=1 / 1=2 与行级过滤（C3），返回 (final_sql, injected_cond, denied_cols, row_rule_summary)。"""
    from ..models import Datasource, TableMeta
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        # 取该 SQL 涉及的数据源（第一张表所属数据源即可；多数据源暂不支持联查）
        tables = _tables_of_query(ast)
        ds_id = None
        for schema, table in tables:
            q = db.query(TableMeta)
            tm = (q.filter(TableMeta.schema_name == schema,
                           TableMeta.table_name == table).first()
                  if schema else q.filter(TableMeta.table_name == table).first())
            if tm:
                ds_id = tm.datasource_id
                break
        if ds_id is None:
            return ast.sql(dialect=dialect), "1=1", [], ""
        workspace_id = db.query(Datasource).get(ds_id).workspace_id
        allow, deny = compute_permissions(user_id, workspace_id)

        denied: list[str] = []
        column_denied: list[tuple[str, str]] = []  # (table, column) 字段级限制，需脱敏
        table_denied: list[str] = []  # 整表限制，注入 1=2
        for schema, table in tables:
            q = db.query(TableMeta)
            tm = (q.filter(TableMeta.schema_name == schema,
                           TableMeta.table_name == table).first()
                  if schema else q.filter(TableMeta.table_name == table).first())
            if not tm:
                continue
            # 整表 deny 快速判断：该表全部列都在 deny 中（column_id=NULL 的整表规则），
            # 则无论 SQL 引用什么列（含 COUNT(*)）都拦截，注入 1=2
            from ..models import ColumnMeta
            all_cols = [c.column_name for c in
                        db.query(ColumnMeta).filter(ColumnMeta.table_meta_id == tm.id).all()]
            table_deny_keys = {f"{ds_id}.{tm.schema_name}.{table}.{c}" for c in all_cols}
            if all_cols and table_deny_keys.issubset(deny):
                table_denied.append(table)
                denied.append(f"{table}.*(整表限制)")
                continue
            refs = extract_referenced_columns(ast, tm.schema_name, table, ds_id)
            for col in refs:
                key = f"{ds_id}.{tm.schema_name}.{table}.{col}"
                if key in deny:
                    column_denied.append((table, col))
                    denied.append(f"{table}.{col}")

        # 整表限制表名集合（小写）
        table_denied_set = {t.lower() for t in table_denied}
        has_table_deny = bool(table_denied)
        # 收集 CTE 名称（用于区分真实表和 CTE 别名）
        cte_names: set[str] = set()
        for cte in ast.find_all(exp.CTE):
            if cte.alias:
                cte_names.add(str(cte.alias).lower())

        cond_deny = sqlglot.parse_one("1=2")
        cond_allow = sqlglot.parse_one("1=1")

        def _select_real_tables(select_node: exp.Select) -> set[str]:
            """提取 SELECT 节点直接 FROM/JOIN 涉及的真实表名（排除 CTE 别名，不递归子查询）。"""
            real: set[str] = set()
            # FROM 子句（sqlglot 用 from_ 避免 Python 关键字冲突）
            from_node = select_node.args.get("from_")
            if from_node and isinstance(from_node.this, exp.Table):
                name = str(from_node.this.name).lower()
                if name not in cte_names:
                    real.add(name)
            # JOIN 子句
            joins = select_node.args.get("joins") or []
            for j in joins:
                if isinstance(j.this, exp.Table):
                    name = str(j.this.name).lower()
                    if name not in cte_names:
                        real.add(name)
            return real

        # 字段级脱敏：所有 SELECT 投影中引用被限制字段的列替换为 '****'（保留别名）
        if column_denied:
            _mask_columns_in_projections(ast, column_denied)

        def inject(node: exp.Expression) -> None:
            """只对涉及整表限制表的 SELECT 注入 1=2，其余注入 1=1。"""
            touches_denied = bool(_select_real_tables(node) & table_denied_set)
            cond = cond_deny.copy() if touches_denied else cond_allow.copy()
            existing = node.args.get("where")
            if existing is not None:
                node.args["where"] = exp.Where(this=exp.and_(existing.this, cond))
            else:
                node.args["where"] = exp.Where(this=cond)

        for node in ast.walk():
            if isinstance(node, exp.Select):
                inject(node)

        # 整体注入标记：有整表限制则为 1=2（仅用于 trace 展示，实际按 SELECT 粒度注入）
        injected = "1=2" if has_table_deny else "1=1"

        # ===== 行级权限注入（C3）=====
        row_summary = ""
        if ds_id is not None:
            row_summary = _apply_row_rules(
                ast, dialect, user_id, ds_id, workspace_id,
                table_denied_set=table_denied_set,
                denied_cols_by_table=_collect_denied_cols(db, ds_id, tables, deny),
                cond_deny=cond_deny, cond_allow=cond_allow)

        final = ast.sql(dialect=dialect, pretty=True)
        return final, injected, denied, row_summary
    finally:
        db.close()


# ========== 行级权限（C3）==========

class RowRule:
    """行级规则（解析后的内存对象）。"""
    __slots__ = ("rule_id", "rule_type", "condition", "condition_type", "note")

    def __init__(self, rule_id: int, rule_type: str, condition: str,
                 condition_type: str, note: str = ""):
        self.rule_id = rule_id
        self.rule_type = rule_type          # allow / deny
        self.condition = condition          # 条件文本（SQL 或模板）
        self.condition_type = condition_type  # sql / template
        self.note = note


def _user_scope_ids(user_id: int, workspace_id: int) -> tuple[set[int], set[int]]:
    """返回用户生效 (role_ids, group_ids)。"""
    from ..models import RoleUser, RoleUserGroup, UserGroupMember

    db = SessionLocal()
    try:
        user = db.query(sqlalchemy_user_model()).get(user_id)
        role_ids: set[int] = set()
        group_ids: set[int] = set()
        if user:
            if user.role_id:
                role_ids.add(user.role_id)
            for ru in db.query(RoleUser).filter(RoleUser.user_id == user_id).all():
                role_ids.add(ru.role_id)
            group_ids = {m.group_id for m in db.query(UserGroupMember)
                         .filter(UserGroupMember.user_id == user_id).all()}
            if group_ids:
                for rg in (db.query(RoleUserGroup)
                           .filter(RoleUserGroup.group_id.in_(group_ids)).all()):
                    role_ids.add(rg.role_id)
        return role_ids, group_ids
    finally:
        db.close()


def compute_row_rules(user_id: int, workspace_id: int, datasource_id: int,
                      table_id: int) -> list[RowRule]:
    """返回该表对用户生效的行级规则（作用域 user/role/group 并集，deny 优先排序）。"""
    from ..models import PermissionRule

    role_ids, group_ids = _user_scope_ids(user_id, workspace_id)
    db = SessionLocal()
    try:
        rules = (db.query(PermissionRule)
                 .filter(PermissionRule.workspace_id == workspace_id,
                         PermissionRule.datasource_id == datasource_id,
                         PermissionRule.table_id == table_id,
                         PermissionRule.enabled.is_(True),
                         PermissionRule.row_enabled.is_(True))
                 .all())
        out: list[RowRule] = []
        for r in rules:
            if not (r.row_filter or "").strip():
                continue
            hit = (r.scope_type == "user" and r.scope_id == user_id) \
                or (r.scope_type == "role" and r.scope_id in role_ids) \
                or (r.scope_type == "group" and r.scope_id in group_ids)
            if hit:
                out.append(RowRule(r.id, r.rule_type, r.row_filter, r.row_filter_type or "sql",
                                   r.row_filter_note or ""))
        # deny 优先（黑名单优先）
        out.sort(key=lambda x: (0 if x.rule_type == "deny" else 1, x.rule_id))
        return out
    finally:
        db.close()


def _resolve_row_template(condition: str, user_id: int, group_ids: set[int],
                          role_code: str) -> str:
    """模板占位替换：{user_id}/{group_ids}/{role_code}（角色编码走单引号转义）。"""
    text = condition
    text = text.replace("{user_id}", str(user_id))
    gids = ", ".join(str(g) for g in sorted(group_ids)) if group_ids else "0"
    text = text.replace("{group_ids}", gids)
    if "{role_code}" in text:
        safe = role_code.replace("'", "''")
        text = text.replace("{role_code}", f"'{safe}'")
    return text


def _build_row_condition(rules: list[RowRule], user_id: int, group_ids: set[int],
                         role_code: str, dialect: str) -> tuple[exp.Expression | None, list[str]]:
    """合并行级条件：visible = (OR allow) AND NOT (OR deny)。
    返回 (表达式, 摘要列表)；无法解析的规则跳过。"""
    allow_exprs: list[exp.Expression] = []
    deny_exprs: list[exp.Expression] = []
    summaries: list[str] = []
    for r in rules:
        cond_text = r.condition
        if r.condition_type == "template":
            cond_text = _resolve_row_template(cond_text, user_id, group_ids, role_code)
        try:
            cond = sqlglot.parse_one(cond_text, read=dialect)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[row] 行级条件解析失败 rule#%s: %s", r.rule_id, exc)
            continue
        if not isinstance(cond, exp.Expression):
            continue
        summaries.append(f"row_rule#{r.rule_id}:{cond_text[:60]}")
        if r.rule_type == "deny":
            deny_exprs.append(cond)
        else:
            allow_exprs.append(cond)
    if not allow_exprs and not deny_exprs:
        return None, []
    parts: list[exp.Expression] = []
    if allow_exprs:
        parts.append(exp.or_(*allow_exprs) if len(allow_exprs) > 1 else allow_exprs[0])
    if deny_exprs:
        d = exp.or_(*deny_exprs) if len(deny_exprs) > 1 else deny_exprs[0]
        parts.append(exp.Not(this=d))
    return (exp.and_(*parts) if len(parts) > 1 else parts[0]), summaries


def _and_to_where(select: exp.Select, cond: exp.Expression) -> None:
    existing = select.args.get("where")
    if existing is not None:
        select.args["where"] = exp.Where(this=exp.and_(existing.this, cond))
    else:
        select.args["where"] = exp.Where(this=cond)


def _inject_table_condition(select: exp.Select, table_name: str, cond: exp.Expression) -> None:
    """行级条件注入：FROM 首表 → WHERE；JOIN 表 → ON（保留 LEFT JOIN 语义）；无 ON 的 JOIN → WHERE。"""
    from_node = select.args.get("from_")
    if from_node and isinstance(from_node.this, exp.Table) \
            and str(from_node.this.name).lower() == table_name.lower():
        _and_to_where(select, cond)
        return
    for j in (select.args.get("joins") or []):
        if isinstance(j.this, exp.Table) and str(j.this.name).lower() == table_name.lower():
            on = j.args.get("on")
            if on is None:
                _and_to_where(select, cond)
            else:
                j.args["on"] = exp.and_(on, cond)
            return


def _collect_denied_cols(db, ds_id: int, tables: list[tuple[str, str]],
                         deny: set[str]) -> dict[str, set[str]]:
    """列级 deny 命中列：小写表名 -> 小写列名集合（供行级条件冲突保守策略使用）。"""
    from ..models import TableMeta, ColumnMeta

    out: dict[str, set[str]] = {}
    for schema, table in tables:
        tm = (db.query(TableMeta)
              .filter(TableMeta.schema_name == schema, TableMeta.table_name == table).first()
              if schema else db.query(TableMeta).filter(TableMeta.table_name == table).first())
        if not tm:
            continue
        cols = {c.column_name for c in
                db.query(ColumnMeta).filter(ColumnMeta.table_meta_id == tm.id).all()}
        denied = {c for c in cols if f"{ds_id}.{tm.schema_name}.{tm.table_name}.{c}" in deny}
        if denied:
            out[table.lower()] = {c.lower() for c in denied}
    return out


def _apply_row_rules(ast: exp.Expression, dialect: str, user_id: int, ds_id: int,
                     workspace_id: int, table_denied_set: set[str],
                     denied_cols_by_table: dict[str, set[str]],
                     cond_deny: exp.Expression, cond_allow: exp.Expression) -> str:
    """为查询涉及的每张表注入行级条件；返回摘要（";" 连接，≤256 字符）。"""
    from ..models import Role, TableMeta

    db = SessionLocal()
    try:
        role_code = ""
        user = db.query(sqlalchemy_user_model()).get(user_id)
        if user:
            role = db.query(Role).get(user.role_id)
            role_code = role.code if role else ""
        group_ids = _user_scope_ids(user_id, workspace_id)[1]
        table_id_map: dict[str, int] = {}
        for tm in db.query(TableMeta).filter(TableMeta.datasource_id == ds_id).all():
            table_id_map[tm.table_name.lower()] = tm.id
    finally:
        db.close()

    cte_names = {str(c.alias).lower() for c in ast.find_all(exp.CTE) if c.alias}
    summaries: list[str] = []
    for select in ast.find_all(exp.Select):
        tables: list[str] = []
        from_node = select.args.get("from_")
        if from_node and isinstance(from_node.this, exp.Table):
            tables.append(str(from_node.this.name))
        for j in (select.args.get("joins") or []):
            if isinstance(j.this, exp.Table):
                tables.append(str(j.this.name))
        for tname in tables:
            tkey = tname.lower()
            if tkey in cte_names or tkey in table_denied_set:
                continue
            table_id = table_id_map.get(tkey)
            if not table_id:
                continue
            rules = compute_row_rules(user_id, workspace_id, ds_id, table_id)
            if not rules:
                continue
            cond, sums = _build_row_condition(rules, user_id, group_ids, role_code, dialect)
            if cond is None:
                continue
            # 保守策略：行条件引用的列若被列级 deny → 整表拒绝（1=2）
            ref_cols = {str(c.name).lower() for c in cond.find_all(exp.Column)}
            if ref_cols & denied_cols_by_table.get(tkey, set()):
                _inject_table_condition(select, tname, cond_deny.copy())
                summaries.append(f"row_rule(列级冲突→整表拒绝):{tname}")
                continue
            _inject_table_condition(select, tname, cond)
            summaries.extend(sums)
    return ";".join(summaries)[:256]
