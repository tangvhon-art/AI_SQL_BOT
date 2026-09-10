"""数据库引擎与会话；默认 MySQL(AI_Infra)，连接失败自动降级 SQLite（开发演示）。
包含：全局软删除过滤、审计字段自动填充（create_user/update_user/delete_time）、
历史表结构自动迁移（对齐团队 DDL 规范）。"""
import logging
import threading
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings

logger = logging.getLogger(__name__)

# ---------- 命名约定（对齐团队规范：idx_ / uk_ / fk_ / pk_ / ck_） ----------
NAMING_CONVENTION = {
    "ix": "idx_%(column_0_label)s",
    "uq": "uk_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _build_engine():
    settings = get_settings()
    url = settings.sqlalchemy_url
    kwargs = {"pool_pre_ping": True, "echo": False}
    if url.startswith("mysql"):
        try:
            engine = create_engine(url, **kwargs)
            with engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            logger.info("元数据库连接成功: MySQL %s@%s:%s/%s",
                        settings.db_user, settings.db_host, settings.db_port, settings.db_name)
            return engine
        except Exception as exc:  # noqa: BLE001
            logger.warning("MySQL 连接失败(%s)，开发模式降级 SQLite 演示库", exc)
            fallback = "sqlite:///./ai_infra.db"
            engine = create_engine(fallback, connect_args={"check_same_thread": False})
            return engine
    engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
    return engine


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


# ---------- 审计字段上下文（当前请求用户，供 before_flush 自动填充） ----------
_audit_ctx = threading.local()


def set_current_user(user_id: int) -> None:
    """记录当前请求用户 ID（在 get_current_user 依赖中调用）。"""
    _audit_ctx.user_id = user_id


def get_current_user_id() -> int:
    """当前请求用户 ID；无请求上下文（种子/后台线程）返回 0。"""
    return getattr(_audit_ctx, "user_id", 0) or 0


_audit_events_installed = False


def _install_audit_events() -> None:
    """安装审计字段自动填充事件（幂等）：
    - 新增对象：create_user / update_user 缺省时填当前用户
    - 更新对象：update_user 填当前用户；is_deleted 翻转时维护 delete_time"""
    global _audit_events_installed
    if _audit_events_installed:
        return
    _audit_events_installed = True

    from sqlalchemy import event
    from sqlalchemy.orm import Session

    @event.listens_for(Session, "before_flush")
    def _fill_audit_fields(session, flush_context, instances):  # noqa: ANN001
        uid = get_current_user_id()
        now = datetime.utcnow()
        for obj in session.new:
            if not isinstance(obj, Base):
                continue
            if hasattr(obj, "create_user") and not obj.create_user:
                obj.create_user = uid
            if hasattr(obj, "update_user") and not obj.update_user:
                obj.update_user = uid
        for obj in session.dirty:
            if not isinstance(obj, Base):
                continue
            if uid and hasattr(obj, "update_user"):
                obj.update_user = uid
            if hasattr(obj, "is_deleted") and hasattr(obj, "delete_time"):
                if obj.is_deleted and obj.delete_time is None:
                    obj.delete_time = now
                elif not obj.is_deleted and obj.delete_time is not None:
                    obj.delete_time = None


# ---------- 软删除全局机制 ----------
# 所有 ORM SELECT 自动追加 is_deleted = 0 条件（含 query().get()/first()/all()、
# select()、子查询、引擎层 SessionLocal 查询）。
# 需要查询已删除记录时使用: session.query(X).execution_options(include_deleted=True)

_soft_delete_filter_installed = False


def install_soft_delete_filter() -> None:
    """安装全局软删除过滤事件（幂等）：
    对 SELECT 语句涉及的每个 ORM 实体注入 is_deleted = 0 条件。
    覆盖 query().get()/first()/all()/join()、select()、引擎层 SessionLocal 查询。
    需要查询已删除记录时: session.query(X).execution_options(include_deleted=True)"""
    global _soft_delete_filter_installed
    if _soft_delete_filter_installed:
        return
    _soft_delete_filter_installed = True

    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from .models import AuditMixin

    @event.listens_for(Session, "do_orm_execute")
    def _apply_soft_delete(execute_state):  # noqa: ANN001
        if not execute_state.is_select or execute_state.is_column_load:
            return
        if execute_state.execution_options.get("include_deleted"):
            return
        stmt = execute_state.statement
        injected = False
        for desc in getattr(stmt, "column_descriptions", ()) or ():
            cls = desc.get("entity")
            if (isinstance(cls, type) and issubclass(cls, AuditMixin)
                    and "is_deleted" in cls.__table__.columns):
                stmt = stmt.where(cls.is_deleted.is_(False))
                injected = True
        if injected:
            execute_state.statement = stmt


def soft_delete_all(query) -> None:
    """批量逻辑删除：置 is_deleted=1 并刷新 update_time / delete_time。"""
    now = datetime.utcnow()
    query.update({"is_deleted": True, "update_time": now, "delete_time": now},
                  synchronize_session=False)


# ---------- 历史表结构迁移（对齐团队 DDL 规范，幂等，仅 MySQL） ----------
def _migrate_meta_tables() -> None:
    """将历史建表迁移到规范结构（每次启动执行，幂等）：
    1) 表名 user -> users；2) created_at -> create_time；
    3) 补 create_user / update_user / delete_time；4) 时间列默认值/ON UPDATE 对齐；
    5) is_deleted 对齐 TINYINT UNSIGNED；6) 表注释；7) 索引/唯一约束命名对齐（idx_/uk_）。"""
    if engine.dialect.name != "mysql":
        return
    from sqlalchemy import inspect

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        # 迁移期间临时关闭外键检查（索引/唯一约束重命名可能被 FK 引用）
        conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")

        # 0) 移除数据库级外键约束（团队规范：禁止数据库外键，仅保留 xxx_id 字段，关联逻辑在 ORM 层）
        fk_rows = conn.exec_driver_sql(
            "SELECT TABLE_NAME, CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE CONSTRAINT_SCHEMA = DATABASE() AND CONSTRAINT_TYPE = 'FOREIGN KEY'"
        ).fetchall()
        for fk_table, fk_name in fk_rows:
            conn.exec_driver_sql(f"ALTER TABLE `{fk_table}` DROP FOREIGN KEY `{fk_name}`")
        # 1) 表名规范：user -> users（若 users 已存在则合并旧数据后删除旧表）
        if "user" in tables and "users" not in tables:
            conn.exec_driver_sql("RENAME TABLE `user` TO `users`")
            tables.discard("user")
            tables.add("users")
        elif "user" in tables and "users" in tables:
            conn.exec_driver_sql(
                "INSERT INTO users (username, password_hash, role_id, workspace_id, display_name, status, "
                "create_time, update_time, create_user, update_user, is_deleted, delete_time) "
                "SELECT u.username, u.password_hash, u.role_id, u.workspace_id, u.display_name, "
                "COALESCE(u.status, 1), COALESCE(u.created_at, CURRENT_TIMESTAMP), "
                "COALESCE(u.update_time, CURRENT_TIMESTAMP), 0, 0, u.is_deleted, NULL "
                "FROM `user` u LEFT JOIN users s ON s.username = u.username WHERE s.id IS NULL")
            conn.exec_driver_sql("DROP TABLE `user`")
            tables.discard("user")
        for table in Base.metadata.sorted_tables:
            name = table.name
            if name not in tables:
                continue
            cols = {c["name"] for c in insp.get_columns(name)}

            # 2) created_at -> create_time
            if "created_at" in cols and "create_time" not in cols:
                conn.exec_driver_sql(
                    f"UPDATE `{name}` SET `created_at` = CURRENT_TIMESTAMP WHERE `created_at` IS NULL")
                conn.exec_driver_sql(
                    f"ALTER TABLE `{name}` CHANGE COLUMN `created_at` `create_time` "
                    "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'")
                cols.discard("created_at")
                cols.add("create_time")

            # 3) 补充审计列
            for col_name, ddl in (
                ("create_user", "BIGINT NOT NULL DEFAULT 0 COMMENT '创建人ID'"),
                ("update_user", "BIGINT NOT NULL DEFAULT 0 COMMENT '更新人ID'"),
                ("delete_time", "DATETIME NULL COMMENT '删除时间'"),
            ):
                if col_name in table.c and col_name not in cols:
                    conn.exec_driver_sql(f"ALTER TABLE `{name}` ADD COLUMN `{col_name}` {ddl}")

            # 4) 时间列默认值 / ON UPDATE 对齐
            if "create_time" in table.c and "create_time" in cols:
                conn.exec_driver_sql(
                    f"UPDATE `{name}` SET `create_time` = CURRENT_TIMESTAMP WHERE `create_time` IS NULL")
                conn.exec_driver_sql(
                    f"ALTER TABLE `{name}` MODIFY COLUMN `create_time` "
                    "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'")
            if "update_time" in table.c and "update_time" in cols:
                conn.exec_driver_sql(
                    f"UPDATE `{name}` SET `update_time` = CURRENT_TIMESTAMP WHERE `update_time` IS NULL")
                conn.exec_driver_sql(
                    f"ALTER TABLE `{name}` MODIFY COLUMN `update_time` "
                    "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'")

            # 5) is_deleted 对齐 TINYINT UNSIGNED
            if "is_deleted" in table.c and "is_deleted" in cols:
                conn.exec_driver_sql(
                    f"ALTER TABLE `{name}` MODIFY COLUMN `is_deleted` "
                    "TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '是否删除 0未删 1已删'")

            # 6) 表注释
            if table.comment:
                conn.exec_driver_sql(f"ALTER TABLE `{name}` COMMENT = '{table.comment}'")

            # 7) 索引 / 唯一约束命名统一规范化（ix_/uq_/fk_/裸列名 -> idx_/uk_）
            seen_idx = {i["name"] for i in insp.get_indexes(name) if i.get("name")}
            for idx in list(insp.get_indexes(name)):
                iname = idx.get("name") or ""
                if not iname or iname == "PRIMARY" or iname.startswith(("idx_", "uk_")):
                    continue
                col_list = idx.get("column_names") or []
                if not col_list:
                    continue
                col_sql = ", ".join(f"`{c}`" for c in col_list)
                if iname.startswith("ix_") and col_list == ["is_deleted"]:
                    new_name = f"idx_{name}_is_deleted"  # 与 naming_convention 生成名一致
                else:
                    new_name = f"uk_{col_list[0]}" if idx.get("unique") else f"idx_{'_'.join(col_list)}"
                if new_name in seen_idx:  # 目标名已存在则跳过（避免冲突）
                    continue
                conn.exec_driver_sql(f"ALTER TABLE `{name}` DROP INDEX `{iname}`")
                if idx.get("unique"):
                    conn.exec_driver_sql(
                        f"ALTER TABLE `{name}` ADD UNIQUE KEY `{new_name}` ({col_sql})")
                else:
                    conn.exec_driver_sql(
                        f"ALTER TABLE `{name}` ADD INDEX `{new_name}` ({col_sql})")
                seen_idx.add(new_name)

            # 8) query_log 扩展列（AI 问数重构：L2-L4 中间产物留痕）
            if name == "query_log":
                for col_name, ddl in (
                    ("spec_json", "JSON NULL COMMENT 'QuerySpec快照'"),
                    ("mapping_json", "JSON NULL COMMENT '数据源映射快照'"),
                    ("anomaly_json", "JSON NULL COMMENT '异常标记列表'"),
                    ("clarify_count", "INT NOT NULL DEFAULT 0 COMMENT '澄清轮数'"),
                    ("fallback", "TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '是否降级模式'"),
                    ("summary_sections_json", "JSON NULL COMMENT '四层结论快照'"),
                    # 能力补建扩展（C1/C2/C6/C8/C11/C14）
                    ("cache_hit", "TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '是否命中缓存'"),
                    ("cache_key", "VARCHAR(128) NOT NULL DEFAULT '' COMMENT '命中的缓存键'"),
                    ("cost_estimate_json", "JSON NULL COMMENT '成本预估明细'"),
                    ("cost_warning", "TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '成本预警标记'"),
                    ("timeout", "TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '是否执行超时'"),
                    ("limited", "TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '是否被限流'"),
                    ("candidates_json", "JSON NULL COMMENT '多候选评分与选中顺序'"),
                    ("scene_code", "VARCHAR(32) NOT NULL DEFAULT '' COMMENT '命中的场景编码'"),
                    ("row_rule_summary", "VARCHAR(256) NOT NULL DEFAULT '' COMMENT '行级注入摘要'"),
                ):
                    if col_name not in cols:
                        conn.exec_driver_sql(f"ALTER TABLE `{name}` ADD COLUMN `{col_name}` {ddl}")

            # 9) permission_rule 行级权限扩展（C3）
            if name == "permission_rule":
                for col_name, ddl in (
                    ("row_filter", "TEXT NULL COMMENT '行过滤条件（SQL条件文本或模板文本）'"),
                    ("row_filter_type", "VARCHAR(8) NOT NULL DEFAULT 'sql' COMMENT '行过滤类型 sql/template'"),
                    ("row_filter_note", "VARCHAR(256) NOT NULL DEFAULT '' COMMENT '行过滤说明'"),
                    ("row_enabled", "TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '行级规则是否启用'"),
                ):
                    if col_name not in cols:
                        conn.exec_driver_sql(f"ALTER TABLE `{name}` ADD COLUMN `{col_name}` {ddl}")

            # 10) column_meta 样例值冗余字段（C7）
            if name == "column_meta":
                if "samples_json" not in cols:
                    conn.exec_driver_sql(
                        f"ALTER TABLE `{name}` ADD COLUMN `samples_json` "
                        "JSON NULL COMMENT '样例值冗余（column_sample 表为主）'")

            # 11) query_log 数据源 ID（C8 血缘挖掘）
            if name == "query_log":
                if "datasource_id" not in cols:
                    conn.exec_driver_sql(
                        f"ALTER TABLE `{name}` ADD COLUMN `datasource_id` "
                        "BIGINT NULL COMMENT '数据源ID（血缘挖掘用）'")

            # 12) eval_result sql_correct 允许 NULL（三态：未判定/通过/未通过）
            if name == "eval_result":
                conn.exec_driver_sql(
                    "ALTER TABLE `eval_result` MODIFY COLUMN `sql_correct` "
                    "BOOLEAN NULL COMMENT 'SQL是否正确（NULL=未判定，需填写期望SQL）'")

            # 13) query_log 多查询拆解结果（C11）
            if name == "query_log":
                if "multi_query_json" not in cols:
                    conn.exec_driver_sql(
                        f"ALTER TABLE `{name}` ADD COLUMN `multi_query_json` "
                        "JSON NULL COMMENT '多查询拆解结果（C11）'")

            # 14) eval_case 评测扩展字段（多查询/图表/解读评测）
            if name == "eval_case":
                for col_name, ddl in (
                    ("eval_mode", "VARCHAR(20) NOT NULL DEFAULT 'sql_only' COMMENT '评测模式'"),
                    ("expect_sub_query_count", "INT NULL COMMENT '期望子查询数量'"),
                    ("expect_sub_queries", "JSON NULL COMMENT '期望子查询详情'"),
                    ("expect_chart_type", "VARCHAR(20) NULL COMMENT '期望图表类型'"),
                    ("expect_chart_types", "JSON NULL COMMENT '多查询期望图表类型列表'"),
                    ("expect_interpretation_points", "JSON NULL COMMENT '期望解读关键点列表'"),
                ):
                    if col_name not in cols:
                        conn.exec_driver_sql(f"ALTER TABLE `{name}` ADD COLUMN `{col_name}` {ddl}")

            # 15) eval_result 评测扩展字段
            if name == "eval_result":
                for col_name, ddl in (
                    ("decomposition_score", "FLOAT NOT NULL DEFAULT 0 COMMENT '拆解评分0-1'"),
                    ("chart_correct", "BOOLEAN NOT NULL DEFAULT FALSE COMMENT '图表推荐是否正确'"),
                    ("interpretation_coverage", "FLOAT NOT NULL DEFAULT 0 COMMENT '解读关键点覆盖率0-1'"),
                    ("interpretation_score", "FLOAT NOT NULL DEFAULT 0 COMMENT '解读质量评分1-5'"),
                    ("prompt_template_id", "BIGINT NULL COMMENT '使用的解读模板ID（A/B评测）'"),
                ):
                    if col_name not in cols:
                        conn.exec_driver_sql(f"ALTER TABLE `{name}` ADD COLUMN `{col_name}` {ddl}")

            # 16) eval_run 评测扩展字段
            if name == "eval_run":
                for col_name, ddl in (
                    ("eval_mode", "VARCHAR(20) NOT NULL DEFAULT 'sql_only' COMMENT '批次评测模式'"),
                    ("prompt_template_ids", "JSON NULL COMMENT 'A/B评测选择的模板ID列表'"),
                    ("total_timeout_ms", "INT NOT NULL DEFAULT 60000 COMMENT '多查询整体超时毫秒'"),
                ):
                    if col_name not in cols:
                        conn.exec_driver_sql(f"ALTER TABLE `{name}` ADD COLUMN `{col_name}` {ddl}")
        # 恢复外键检查
        conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


def _seed_builtin_prompts() -> None:
    """初始化内置 Prompt 模板（ai_interpret 场景），仅在无任何内置模板时插入。"""
    from .models import Prompt
    builtin = [
        {
            "name": "通用分析（默认）",
            "description": "通用数据分析模板，平衡全面，适用于大多数场景",
            "scene_tags": "通用",
            "prompt_template": (
                "你是一位资深数据分析师。请基于以下数据进行分析：\n"
                "原始问题：{{question}}\n"
                "数据摘要：{{data_summary}}\n"
                "关键指标：{{metrics}}\n\n"
                "请按以下结构输出分析结果（JSON）：\n"
                '{"summary": "总体结论（1-3句）", '
                '"key_metrics": [{"name":"指标名","value":"值","change":"变化"}], '
                '"trends": ["趋势分析"], "comparisons": ["对比发现"], '
                '"anomalies": [{"desc":"异常描述","severity":"high/medium/low"}], '
                '"suggestions": ["行动建议（1-3条）"]}'
            ),
            "output_format": "structured_json",
            "is_default": True,
        },
        {
            "name": "经营分析",
            "description": "适用于门店经营、销售、库存等经营场景，侧重原因推测和改进建议",
            "scene_tags": "经营,销售,门店",
            "prompt_template": (
                "你是一位资深经营分析师。请基于以下经营数据进行分析：\n"
                "原始问题：{{question}}\n"
                "数据摘要：{{data_summary}}\n"
                "关键指标：{{metrics}}\n\n"
                "请从经营角度分析：①整体表现 ②亮点与不足 ③原因推测 ④改进建议\n"
                "输出 JSON：{\"summary\":\"\", \"key_metrics\":[], \"trends\":[], "
                "\"comparisons\":[], \"anomalies\":[], \"suggestions\":[]}"
            ),
            "output_format": "structured_json",
            "is_default": False,
        },
        {
            "name": "财务分析",
            "description": "适用于财务、成本、利润等场景，严谨精确，侧重风险提示",
            "scene_tags": "财务,成本,利润",
            "prompt_template": (
                "你是一位资深财务分析师。请基于以下财务数据进行严谨分析：\n"
                "原始问题：{{question}}\n"
                "数据摘要：{{data_summary}}\n"
                "关键指标：{{metrics}}\n\n"
                "请分析：①财务健康度 ②成本结构 ③盈利分析 ④风险提示\n"
                "所有数字必须来自数据，不得编造。输出 JSON："
                "{\"summary\":\"\", \"key_metrics\":[], \"trends\":[], "
                "\"comparisons\":[], \"anomalies\":[], \"suggestions\":[]}"
            ),
            "output_format": "structured_json",
            "is_default": False,
        },
        {
            "name": "运营分析",
            "description": "适用于运营、流程、效率等场景，侧重瓶颈识别和可操作建议",
            "scene_tags": "运营,流程,效率",
            "prompt_template": (
                "你是一位资深运营分析师。请基于以下运营数据进行分析：\n"
                "原始问题：{{question}}\n"
                "数据摘要：{{data_summary}}\n"
                "关键指标：{{metrics}}\n\n"
                "请分析：①效率指标 ②瓶颈识别 ③流程优化方向 ④可执行建议\n"
                "输出 JSON：{\"summary\":\"\", \"key_metrics\":[], \"trends\":[], "
                "\"comparisons\":[], \"anomalies\":[], \"suggestions\":[]}"
            ),
            "output_format": "structured_json",
            "is_default": False,
        },
        {
            "name": "简洁摘要",
            "description": "3句话核心结论+关键数字，适合快速汇报",
            "scene_tags": "通用,汇报",
            "prompt_template": (
                "你是一位数据分析师。请基于以下数据给出简洁摘要：\n"
                "原始问题：{{question}}\n"
                "数据摘要：{{data_summary}}\n\n"
                "要求：不超过3句话，包含最关键的数字和结论。\n"
                "输出 JSON：{\"summary\":\"3句话核心结论\", \"key_metrics\":[前3个关键指标], "
                "\"trends\":[], \"comparisons\":[], \"anomalies\":[], \"suggestions\":[]}"
            ),
            "output_format": "structured_json",
            "is_default": False,
        },
    ]
    try:
        with SessionLocal() as db:
            existing = db.query(Prompt).filter(Prompt.is_builtin.is_(True)).first()
            if existing:
                return
            for i, item in enumerate(builtin):
                db.add(Prompt(
                    workspace_id=0,  # workspace_id=0 表示全局内置，查询时按 workspace_id IN (0, 当前ws)
                    scene_type="ai_interpret",
                    name=item["name"],
                    description=item["description"],
                    scene_tags=item["scene_tags"],
                    prompt_template=item["prompt_template"],
                    output_format=item["output_format"],
                    is_default=item["is_default"],
                    is_builtin=True,
                    sort_order=i,
                    created_by=0,
                ))
            db.commit()
    except Exception:
        pass  # 内置模板初始化失败不影响启动


def init_db():
    from . import models  # noqa: F401  确保模型注册
    Base.metadata.create_all(bind=engine)
    _migrate_meta_tables()
    install_soft_delete_filter()
    _install_audit_events()
    _seed_builtin_prompts()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
