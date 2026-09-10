"""SQLAlchemy ORM 模型：对应需求/设计第 8 章数据模型。
对齐团队数据库设计规范：统一审计字段、小写下划线命名、全字段 COMMENT、InnoDB/utf8mb4、
逻辑删除（is_deleted + delete_time）、索引命名约定（idx_/uk_，见 database.Base 的 naming_convention）。"""
from datetime import datetime

from sqlalchemy import (JSON, BigInteger, Boolean, Column, DateTime, Float,
                        Integer, SmallInteger, String, Text,
                        UniqueConstraint, text)
from sqlalchemy.dialects import mysql

from .database import Base

# 主键：MySQL 生成 BIGINT UNSIGNED 自增；SQLite 降级为 INTEGER（INTEGER PRIMARY KEY 才是 rowid 自增）
BIGINT_UID = (BigInteger()
              .with_variant(mysql.BIGINT(unsigned=True), "mysql")
              .with_variant(Integer(), "sqlite"))


class AuditMixin:
    """统一审计字段（所有业务表必须继承）：
    id / create_time / update_time / create_user / update_user / is_deleted / delete_time。"""
    id = Column(BIGINT_UID, primary_key=True, autoincrement=True, comment="主键ID")
    create_time = Column(DateTime, nullable=False, default=datetime.utcnow,
                         server_default=text("CURRENT_TIMESTAMP"), comment="创建时间")
    update_time = Column(DateTime, nullable=False, default=datetime.utcnow,
                         onupdate=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"),
                         server_onupdate=text("CURRENT_TIMESTAMP"), comment="更新时间")
    create_user = Column(BigInteger, nullable=False, default=0,
                         server_default=text("0"), comment="创建人ID")
    update_user = Column(BigInteger, nullable=False, default=0,
                         server_default=text("0"), comment="更新人ID")
    is_deleted = Column(Boolean, nullable=False, default=False, index=True,
                        server_default=text("0"), comment="是否删除 0未删 1已删")
    delete_time = Column(DateTime, nullable=True, comment="删除时间")


# ---------- 组织与账号 ----------
class Workspace(Base, AuditMixin):
    __tablename__ = "workspace"
    __table_args__ = {"comment": "工作空间表"}
    name = Column(String(128), nullable=False, default="", comment="工作空间名称")
    default_llm_config_id = Column(BigInteger, nullable=True, comment="默认LLM配置ID")
    default_embed_config_id = Column(BigInteger, nullable=True, comment="默认Embedding配置ID")


class Role(Base, AuditMixin):
    __tablename__ = "role"
    __table_args__ = {"comment": "角色表"}
    code = Column(String(32), unique=True, nullable=False, comment="角色编码 admin/data_admin/biz_user")
    name = Column(String(64), nullable=False, comment="角色名称")


class User(Base, AuditMixin):
    __tablename__ = "users"
    __table_args__ = {"comment": "用户表"}
    username = Column(String(64), unique=True, nullable=False, comment="用户名")
    password_hash = Column(String(128), nullable=False, comment="密码哈希")
    role_id = Column(BigInteger, nullable=False, comment="角色ID")
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    display_name = Column(String(64), default="", comment="显示名称")
    status = Column(SmallInteger, default=1, comment="状态 1启用 0停用")


# ---------- 组织与权限（角色/用户组/菜单） ----------
class UserGroup(Base, AuditMixin):
    """用户组：角色可分配给用户组，用户通过组间接获得角色与权限。"""
    __tablename__ = "user_group"
    __table_args__ = {"comment": "用户组表"}
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    name = Column(String(64), nullable=False, comment="用户组名称")
    remark = Column(String(255), default="", comment="备注")


class UserGroupMember(Base, AuditMixin):
    """用户组-成员（多对多）。"""
    __tablename__ = "user_group_member"
    __table_args__ = {"comment": "用户组成员关联表"}
    group_id = Column(BigInteger, nullable=False, comment="用户组ID")
    user_id = Column(BigInteger, nullable=False, comment="用户ID")


class RoleUser(Base, AuditMixin):
    """用户-角色（多对多）：用户被直接分配的角色。"""
    __tablename__ = "role_user"
    __table_args__ = {"comment": "用户角色关联表"}
    role_id = Column(BigInteger, nullable=False, comment="角色ID")
    user_id = Column(BigInteger, nullable=False, comment="用户ID")


class RoleUserGroup(Base, AuditMixin):
    """角色-用户组（多对多）：角色分配给用户组。"""
    __tablename__ = "role_user_group"
    __table_args__ = {"comment": "角色用户组关联表"}
    role_id = Column(BigInteger, nullable=False, comment="角色ID")
    group_id = Column(BigInteger, nullable=False, comment="用户组ID")


class Menu(Base, AuditMixin):
    """功能菜单（多级）：用于角色可被分配菜单。"""
    __tablename__ = "menu"
    __table_args__ = {"comment": "功能菜单表"}
    parent_id = Column(BigInteger, default=0, comment="父菜单ID 0=一级菜单")
    code = Column(String(64), unique=True, nullable=False, comment="菜单编码")
    name = Column(String(64), nullable=False, comment="菜单名称")
    path = Column(String(128), default="", comment="路由路径")
    icon = Column(String(64), default="", comment="图标")
    sort_order = Column(Integer, default=0, comment="排序值")


class RoleMenu(Base, AuditMixin):
    """角色-菜单（多对多）。"""
    __tablename__ = "role_menu"
    __table_args__ = {"comment": "角色菜单关联表"}
    role_id = Column(BigInteger, nullable=False, comment="角色ID")
    menu_id = Column(BigInteger, nullable=False, comment="菜单ID")


# ---------- 数据源与元数据 ----------
class Datasource(Base, AuditMixin):
    __tablename__ = "datasource"
    __table_args__ = {"comment": "数据源表"}
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    name = Column(String(128), nullable=False, comment="数据源名称")
    type = Column(String(32), nullable=False, comment="数据源类型 mysql/postgresql/clickhouse")
    host = Column(String(128), nullable=False, comment="主机地址")
    port = Column(Integer, nullable=False, comment="端口")
    db_name = Column(String(128), nullable=False, comment="数据库名")
    user = Column(String(64), nullable=False, comment="连接用户名")
    password_enc = Column(String(512), nullable=False, comment="密码密文")
    params_json = Column(JSON, default=dict, comment="扩展参数JSON")
    status = Column(String(32), default="not_connected", comment="连接状态 not_connected/ok/failed/syncing")
    last_sync_at = Column(DateTime, nullable=True, comment="最近同步时间")


class TableMeta(Base, AuditMixin):
    __tablename__ = "table_meta"
    __table_args__ = (
        UniqueConstraint("datasource_id", "schema_name", "table_name"),
        {"comment": "数据表元信息表"},
    )
    datasource_id = Column(BigInteger, nullable=False, comment="数据源ID")
    schema_name = Column(String(128), default="", comment="Schema名称")
    table_name = Column(String(128), nullable=False, comment="表名")
    comment = Column(String(512), default="", comment="表注释")
    table_type = Column(String(32), default="BASE TABLE", comment="表类型 BASE TABLE/VIEW")
    column_count = Column(Integer, default=0, comment="字段数量")
    deprecated = Column(Boolean, default=False, comment="是否废弃")
    synced_at = Column(DateTime, default=datetime.utcnow, comment="同步时间")


class ColumnMeta(Base, AuditMixin):
    __tablename__ = "column_meta"
    __table_args__ = (
        UniqueConstraint("table_meta_id", "column_name"),
        {"comment": "数据字段元信息表"},
    )
    table_meta_id = Column(BigInteger, nullable=False, comment="表元信息ID")
    column_name = Column(String(128), nullable=False, comment="字段名")
    data_type = Column(String(64), default="", comment="数据类型")
    comment = Column(String(512), default="", comment="字段注释")
    is_nullable = Column(Boolean, default=True, comment="是否可空")
    is_pk = Column(Boolean, default=False, comment="是否主键")
    ordinal = Column(Integer, default=0, comment="字段序号")
    default_value = Column(String(255), default="", comment="默认值")
    samples_json = Column(JSON, default=list, comment="样例值冗余（column_sample 表为主）")


class Relationship(Base, AuditMixin):
    __tablename__ = "relationship"
    __table_args__ = {"comment": "表关系表"}
    datasource_id = Column(BigInteger, nullable=False, comment="数据源ID")
    src_table_id = Column(BigInteger, nullable=False, comment="源表ID")
    src_col_id = Column(BigInteger, nullable=False, comment="源字段ID")
    dst_table_id = Column(BigInteger, nullable=False, comment="目标表ID")
    dst_col_id = Column(BigInteger, nullable=False, comment="目标字段ID")
    rel_type = Column(String(16), default="1:N", comment="关系类型 1:1/1:N/N:M")
    source = Column(String(16), default="manual", comment="来源 manual/fk_auto/heuristic")
    enabled = Column(Boolean, default=True, comment="是否启用")


# ---------- 模型配置 ----------
class ModelConfig(Base, AuditMixin):
    __tablename__ = "model_config"
    __table_args__ = {"comment": "模型配置表"}
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    name = Column(String(128), nullable=False, comment="配置名称")
    provider = Column(String(64), default="openai_compatible", comment="服务商")
    base_url = Column(String(256), nullable=False, comment="API地址")
    api_key_enc = Column(String(512), default="", comment="API密钥密文")
    model_name = Column(String(128), nullable=False, comment="模型名")
    embedding_model = Column(String(128), default="", comment="Embedding模型名")
    temperature = Column(Float, default=0.1, comment="温度参数")
    top_p = Column(Float, default=0.9, comment="Top-P参数")
    max_tokens = Column(Integer, default=2048, comment="最大Token数")
    scene = Column(String(32), default="sql", comment="使用场景 sql/summary")
    is_default = Column(Boolean, default=False, comment="是否默认配置")


# ---------- 知识库 ----------
class FaqPair(Base, AuditMixin):
    __tablename__ = "faq_pair"
    __table_args__ = {"comment": "FAQ问答对表"}
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    question = Column(String(512), nullable=False, comment="问题")
    synonyms_json = Column(JSON, default=list, comment="同义问法JSON")
    answer = Column(Text, default="", comment="答案")
    category = Column(String(64), default="", comment="分类")
    tags = Column(String(255), default="", comment="标签")
    datasource_id = Column(BigInteger, nullable=True, comment="关联数据源ID")
    table_ids = Column(String(255), default="", comment="关联表ID(逗号分隔)")
    sql_example = Column(Text, default="", comment="SQL示例")
    enabled = Column(Boolean, default=True, comment="是否启用")


class KnowledgeDoc(Base, AuditMixin):
    __tablename__ = "knowledge_doc"
    __table_args__ = {"comment": "知识库文档表"}
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    name = Column(String(255), nullable=False, comment="文档名")
    file_type = Column(String(16), default="txt", comment="文件类型")
    file_path = Column(String(512), default="", comment="文件路径")
    size = Column(Integer, default=0, comment="文件大小(字节)")
    chunk_size = Column(Integer, default=600, comment="切片大小")
    overlap = Column(Integer, default=80, comment="切片重叠")
    status = Column(String(32), default="pending", comment="状态 pending/parsing/embedded/failed")
    error_msg = Column(String(512), default="", comment="错误信息")
    version = Column(Integer, default=1, comment="版本号")


class DocChunk(Base, AuditMixin):
    __tablename__ = "doc_chunk"
    __table_args__ = {"comment": "文档切片表"}
    doc_id = Column(BigInteger, nullable=True, comment="文档ID")
    faq_id = Column(BigInteger, nullable=True, comment="FAQ ID")
    seq = Column(Integer, default=0, comment="切片序号")
    content = Column(Text, nullable=False, comment="切片内容")
    token_count = Column(Integer, default=0, comment="Token数")
    embedding_status = Column(String(16), default="pending", comment="向量状态 pending/embedded")
    embedding = Column(Text, default=None, comment="向量JSON(入库存储)")


class SqlExample(Base, AuditMixin):
    __tablename__ = "sql_example"
    __table_args__ = {"comment": "SQL示例表"}
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    question = Column(String(512), nullable=False, comment="问题")
    sql_text = Column(Text, nullable=False, comment="SQL文本")
    dialect = Column(String(32), default="mysql", comment="SQL方言")
    table_ids = Column(String(255), default="", comment="关联表ID(逗号分隔)")
    tags = Column(String(255), default="", comment="标签")
    source = Column(String(32), default="manual", comment="来源 manual/faq/auto")
    enabled = Column(Boolean, default=True, comment="是否启用")


# ---------- 会话与问数 ----------
class Conversation(Base, AuditMixin):
    __tablename__ = "conversation"
    __table_args__ = {"comment": "会话表"}
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    user_id = Column(BigInteger, nullable=False, comment="用户ID")
    title = Column(String(128), default="新会话", comment="会话标题")


class ConversationMessage(Base, AuditMixin):
    __tablename__ = "conversation_message"
    __table_args__ = {"comment": "会话消息表"}
    conversation_id = Column(BigInteger, nullable=False, comment="会话ID")
    role = Column(String(16), nullable=False, comment="角色 user/assistant")
    content_type = Column(String(16), default="text", comment="内容类型 text/sql/table/chart/summary")
    content_json = Column(JSON, default=dict, comment="消息内容JSON")


class QueryLog(Base, AuditMixin):
    __tablename__ = "query_log"
    __table_args__ = {"comment": "问数日志表"}
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    user_id = Column(BigInteger, nullable=False, comment="用户ID")
    datasource_id = Column(BigInteger, nullable=True, comment="数据源ID（血缘挖掘用）")
    conversation_id = Column(BigInteger, nullable=True, comment="会话ID")
    question = Column(String(512), default="", comment="问题")
    intent = Column(String(32), default="", comment="意图类型")
    matched_tables = Column(String(512), default="", comment="匹配表(逗号分隔)")
    generated_sql = Column(Text, default="", comment="生成的SQL")
    permission_injected = Column(String(8), default="", comment="权限注入条件 1=1/1=2")
    executed = Column(Boolean, default=False, comment="是否已执行")
    row_count = Column(Integer, default=0, comment="返回行数")
    latency_ms = Column(Integer, default=0, comment="耗时毫秒")
    chart_type = Column(String(32), default="", comment="图表类型")
    llm_used = Column(Boolean, default=False, comment="是否真实调用大模型")
    feedback = Column(String(8), nullable=True, comment="反馈 good/bad")
    feedback_note = Column(String(512), default="", comment="反馈备注")
    # AI 问数重构扩展字段（L2-L4 中间产物留痕，支撑溯源与迭代）
    spec_json = Column(JSON, default=dict, comment="QuerySpec快照")
    mapping_json = Column(JSON, default=dict, comment="数据源映射快照")
    anomaly_json = Column(JSON, default=list, comment="异常标记列表")
    clarify_count = Column(Integer, default=0, comment="澄清轮数")
    fallback = Column(Boolean, default=False, comment="是否降级模式")
    summary_sections_json = Column(JSON, default=dict, comment="四层结论快照")
    # 能力补建扩展字段（缓存/成本/超时/多候选/场景/行级权限）
    cache_hit = Column(Boolean, default=False, comment="是否命中缓存")
    cache_key = Column(String(128), default="", comment="命中的缓存键")
    cost_estimate_json = Column(JSON, default=dict, comment="成本预估明细")
    cost_warning = Column(Boolean, default=False, comment="成本预警标记")
    timeout = Column(Boolean, default=False, comment="是否执行超时")
    limited = Column(Boolean, default=False, comment="是否被限流")
    candidates_json = Column(JSON, default=list, comment="多候选评分与选中顺序")
    scene_code = Column(String(32), default="", comment="命中的场景编码")
    row_rule_summary = Column(String(256), default="", comment="行级注入摘要")
    multi_query_json = Column(JSON, default=dict, comment="多查询拆解结果（C11）")


# ---------- 结果复用与定时任务 ----------
class SavedQuery(Base, AuditMixin):
    __tablename__ = "saved_query"
    __table_args__ = {"comment": "保存查询表"}
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    owner_id = Column(BigInteger, nullable=False, comment="创建人ID")
    name = Column(String(128), nullable=False, comment="名称")
    sql_text = Column(Text, nullable=False, comment="SQL文本")
    params_json = Column(JSON, default=list, comment="参数定义JSON")
    chart_config_json = Column(JSON, default=dict, comment="图表配置JSON")
    tags = Column(String(255), default="", comment="标签")
    remark = Column(String(512), default="", comment="备注")


class ScheduledTask(Base, AuditMixin):
    __tablename__ = "scheduled_task"
    __table_args__ = {"comment": "定时任务表"}
    saved_query_id = Column(BigInteger, nullable=False, comment="保存查询ID")
    name = Column(String(128), nullable=False, comment="任务名称")
    cron_expr = Column(String(64), nullable=False, comment="Cron表达式")
    timezone = Column(String(64), default="Asia/Shanghai", comment="时区")
    param_values_json = Column(JSON, default=dict, comment="参数值JSON")
    status = Column(String(16), default="enabled", comment="状态 enabled/disabled")
    last_run_at = Column(DateTime, nullable=True, comment="上次运行时间")
    next_run_at = Column(DateTime, nullable=True, comment="下次运行时间")


class TaskRunLog(Base, AuditMixin):
    __tablename__ = "task_run_log"
    __table_args__ = {"comment": "任务运行日志表"}
    task_id = Column(BigInteger, nullable=False, comment="任务ID")
    run_time = Column(DateTime, default=datetime.utcnow, comment="运行时间")
    status = Column(String(16), default="running", comment="状态 running/success/failed")
    param_values_json = Column(JSON, default=dict, comment="参数值JSON")
    row_count = Column(Integer, default=0, comment="返回行数")
    chart_snapshot_json = Column(JSON, default=dict, comment="图表快照JSON")
    latency_ms = Column(Integer, default=0, comment="耗时毫秒")
    error_msg = Column(String(512), default="", comment="错误信息")
    retry_count = Column(Integer, default=0, comment="重试次数")


# ---------- 字段级权限 ----------
class PermissionRule(Base, AuditMixin):
    __tablename__ = "permission_rule"
    __table_args__ = {"comment": "字段权限规则表"}
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    scope_type = Column(String(8), nullable=False, comment="作用域类型 role/user/group")
    scope_id = Column(BigInteger, nullable=False, comment="作用域ID")
    rule_type = Column(String(8), nullable=False, comment="规则类型 allow/deny")
    datasource_id = Column(BigInteger, nullable=False, comment="数据源ID")
    table_id = Column(BigInteger, nullable=False, comment="表ID")
    column_ids = Column(JSON, default=list, comment="字段ID数组，空数组=整表规则")
    enabled = Column(Boolean, default=True, comment="是否启用")
    # 行级权限扩展（C3）：row_enabled=false 时行为与旧版一致
    row_filter = Column(Text, nullable=True, comment="行过滤条件（SQL 条件文本或模板文本）")
    row_filter_type = Column(String(8), default="sql", comment="行过滤类型 sql/template")
    row_filter_note = Column(String(256), default="", comment="行过滤说明（审计展示）")
    row_enabled = Column(Boolean, default=False, comment="行级规则是否启用")


# ---------- 系统配置覆盖（能力补建：平台级配置持久化） ----------
class SystemConfig(Base, AuditMixin):
    __tablename__ = "system_config"
    __table_args__ = (
        UniqueConstraint("key", name="uk_syscfg_key"),
        {"comment": "系统配置覆盖表（平台级，key 为 config 字段名）"},
    )
    section = Column(String(32), nullable=False, comment="配置分组（cache/cost_guard/rate_limit/schema_sync/lineage/eval）")
    key = Column(String(64), nullable=False, comment="config 字段名")
    value_json = Column(JSON, nullable=False, comment="覆盖值")


# ---------- 意图识别词典与句式模板（AI 问数重构：L2 意图理解层配置化） ----------
class IntentDict(Base, AuditMixin):
    __tablename__ = "intent_dict"
    __table_args__ = {"comment": "意图识别词典表"}
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    dict_type = Column(String(16), nullable=False, comment="词典类型 metric/dimension/time/filter/action")
    term = Column(String(64), nullable=False, comment="词条")
    aliases = Column(JSON, default=list, comment="同义词/错别字/拼音(JSON数组)")
    target_table = Column(String(128), default="", comment="目标表名")
    target_column = Column(String(128), default="", comment="目标字段名")
    agg = Column(String(16), default="", comment="默认聚合 sum/count/avg/max/min")
    unit = Column(String(32), default="", comment="单位 万元/元/单/人")
    enabled = Column(Boolean, default=True, comment="是否启用")


class IntentTemplate(Base, AuditMixin):
    __tablename__ = "intent_template"
    __table_args__ = {"comment": "意图句式模板表"}
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    pattern = Column(String(256), nullable=False, comment="句式模板（含{指标}{时间}等占位符）")
    intent = Column(String(16), nullable=False, comment="意图类型 value/compare/ranking/trend/detail/statistic")
    slot_map = Column(JSON, default=dict, comment="占位符→要素映射")
    priority = Column(Integer, default=0, comment="优先级")
    enabled = Column(Boolean, default=True, comment="是否启用")


# ---------- 能力补建（C1/C4/C7/C8/C14）新增表 ----------
class CacheEntry(Base, AuditMixin):
    """SQL/结果缓存条目（C1）：一级生成缓存（相似问句）+ 二级结果缓存（SQL 指纹）。"""
    __tablename__ = "cache_entry"
    __table_args__ = (
        UniqueConstraint("workspace_id", "cache_type", "cache_key"),
        {"comment": "缓存条目表"},
    )
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    cache_type = Column(String(8), nullable=False, comment="缓存类型 gen/result")
    cache_key = Column(String(128), nullable=False, comment="缓存键（相似问句键/SQL指纹）")
    question = Column(String(512), default="", comment="原问题（一级缓存展示用）")
    sql_fingerprint = Column(String(64), default="", comment="SQL指纹（二级缓存）")
    sql_text = Column(Text, default="", comment="生成/执行的SQL")
    datasource_id = Column(BigInteger, default=0, comment="关联数据源ID（失效用）")
    payload_json = Column(JSON, default=dict, comment="生成结果/结果集")
    schema_version = Column(DateTime, nullable=True, comment="生成时Schema版本（synced_at）")
    hit_count = Column(Integer, default=0, comment="命中次数")
    last_hit_at = Column(DateTime, nullable=True, comment="最近命中时间")
    expires_at = Column(DateTime, nullable=True, comment="过期时间")


class EvalCase(Base, AuditMixin):
    """评测用例（C4）：问题 + 期望结果。"""
    __tablename__ = "eval_case"
    __table_args__ = {"comment": "评测用例表"}
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    datasource_id = Column(BigInteger, nullable=False, comment="数据源ID")
    question = Column(String(512), nullable=False, comment="问题")
    expect_tables_json = Column(JSON, default=list, comment="期望表集合")
    expect_metrics_json = Column(JSON, default=list, comment="期望指标列表")
    expect_filters_json = Column(JSON, default=list, comment="期望过滤条件列表")
    expect_sql = Column(Text, default="", comment="期望SQL（可选）")
    scene_code = Column(String(32), default="", comment="场景编码（与C14联动）")
    tags = Column(String(255), default="", comment="标签（逗号分隔）")
    status = Column(String(16), default="active", comment="状态 active/disabled")
    created_by = Column(BigInteger, default=0, comment="创建人ID")


class EvalRun(Base, AuditMixin):
    """评测批次（C4）：一次评测执行的汇总与指标。"""
    __tablename__ = "eval_run"
    __table_args__ = {"comment": "评测批次表"}
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    name = Column(String(128), default="", comment="批次名称")
    scope_json = Column(JSON, default=dict, comment="范围（数据源/标签/用例ID列表）")
    status = Column(String(16), default="running", comment="状态 running/success/failed")
    mock_execute = Column(Boolean, default=True, comment="是否mock执行（不真实连库）")
    eval_mode = Column(String(32), default="sql_only", comment="评测模式 sql_only/full")
    prompt_template_ids = Column(JSON, default=list, comment="使用的提示词模板ID列表")
    total_timeout_ms = Column(Integer, nullable=True, comment="总超时毫秒")
    total = Column(Integer, default=0, comment="用例总数")
    metrics_json = Column(JSON, default=dict, comment="汇总指标（命中率/正确率等）")
    started_at = Column(DateTime, nullable=True, comment="开始时间")
    finished_at = Column(DateTime, nullable=True, comment="结束时间")
    created_by = Column(BigInteger, default=0, comment="创建人ID")


class EvalResult(Base, AuditMixin):
    """评测明细（C4）：单用例判定结果。"""
    __tablename__ = "eval_result"
    __table_args__ = {"comment": "评测结果明细表"}
    run_id = Column(BigInteger, nullable=False, comment="评测批次ID")
    case_id = Column(BigInteger, nullable=False, comment="评测用例ID")
    intent_ok = Column(Boolean, default=False, comment="意图是否正确")
    tables_hit = Column(Boolean, default=False, comment="期望表是否命中")
    sql_generated = Column(Boolean, default=False, comment="是否生成SQL")
    sql_executable = Column(Boolean, default=False, comment="SQL是否可执行")
    sql_correct = Column(Boolean, nullable=True, comment="SQL是否正确（NULL=未判定，需填写期望SQL）")
    e2e_ok = Column(Boolean, default=False, comment="端到端是否正确")
    latency_ms = Column(Integer, default=0, comment="耗时毫秒")
    llm_used = Column(Boolean, default=False, comment="是否调用LLM")
    tokens_json = Column(JSON, default=dict, comment="Token消耗")
    error_msg = Column(String(512), default="", comment="错误信息")
    detail_json = Column(JSON, default=dict, comment="判定明细（期望/实际）")


class ColumnSample(Base, AuditMixin):
    """字段样例值（C7）：Schema 采集的低基数字段样例。"""
    __tablename__ = "column_sample"
    __table_args__ = {"comment": "字段样例值表"}
    column_meta_id = Column(BigInteger, nullable=False, comment="字段元信息ID")
    sample_value = Column(String(128), nullable=False, comment="样例值")
    freq = Column(Integer, default=0, comment="出现频次")
    sample_type = Column(String(8), default="top", comment="采集类型 top/random")
    synced_at = Column(DateTime, default=datetime.utcnow, comment="采集时间")


class LineageEdge(Base, AuditMixin):
    """血缘边（C8）：表/字段级来源-去向关系。"""
    __tablename__ = "lineage_edge"
    __table_args__ = {"comment": "血缘边表"}
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    datasource_id = Column(BigInteger, nullable=False, comment="数据源ID")
    src_table_id = Column(BigInteger, nullable=False, comment="源表ID")
    src_column_id = Column(BigInteger, nullable=True, comment="源字段ID（可空=表级）")
    dst_table_id = Column(BigInteger, nullable=False, comment="目标表ID")
    dst_column_id = Column(BigInteger, nullable=True, comment="目标字段ID（可空=表级）")
    edge_type = Column(String(8), default="query", comment="边类型 view/query/manual")
    source = Column(String(8), default="manual", comment="来源 ddl/query_log/manual")
    confidence = Column(Integer, default=100, comment="置信度 0-100")
    last_seen_at = Column(DateTime, nullable=True, comment="最后发现时间")


class SceneDef(Base, AuditMixin):
    """场景模板（C14）：指标包 + 提示词模板 + 示例 + 报告模板。workspace_id 为空=系统预置。"""
    __tablename__ = "scene_def"
    __table_args__ = (
        UniqueConstraint("workspace_id", "scene_code"),
        {"comment": "场景模板表"},
    )
    workspace_id = Column(BigInteger, nullable=True, comment="工作空间ID（空=系统预置）")
    scene_code = Column(String(32), nullable=False, comment="场景编码")
    scene_name = Column(String(64), nullable=False, comment="场景名称")
    description = Column(String(255), default="", comment="场景描述")
    metric_pack_json = Column(JSON, default=list, comment="指标包（建议指标/维度/口径）")
    gen_prompt_template = Column(Text, default="", comment="生成提示词模板")
    explain_template = Column(Text, default="", comment="解释/报告模板")
    examples_json = Column(JSON, default=list, comment="场景示例（问题-SQL对）")
    report_template = Column(Text, default="", comment="报告模板（auto_report用）")
    enabled = Column(Boolean, default=True, comment="是否启用")
    sort_order = Column(Integer, default=0, comment="排序值")


# ---------- 报告管理（多查询+AI解读） ----------
class Report(Base, AuditMixin):
    """问数报告：保存完整的 Dashboard + AI 解读快照。"""
    __tablename__ = "report"
    __table_args__ = {"comment": "问数报告表"}
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    title = Column(String(200), nullable=False, comment="报告标题")
    original_question = Column(Text, default="", comment="原始问题")
    multi_query_spec = Column(JSON, default=dict, comment="多查询拆解结果快照")
    dashboard_data = Column(JSON, default=dict, comment="Dashboard布局+卡片数据快照")
    ai_interpretation = Column(JSON, default=dict, comment="AI解读结构化结果")
    interpretation_text = Column(Text, default="", comment="解读纯文本（搜索/复制/导出用）")
    remark = Column(Text, default="", comment="用户备注")
    created_by = Column(BigInteger, default=0, comment="创建人ID")


# ---------- 公共 Prompt 管理 ----------
class Prompt(Base, AuditMixin):
    """公共 Prompt 模板：多场景（AI解读/洞察草案/SQL生成等）。"""
    __tablename__ = "prompt"
    __table_args__ = {"comment": "公共Prompt模板表"}
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    scene_type = Column(String(50), nullable=False, default="ai_interpret",
                        comment="使用场景：ai_interpret/insight_draft/sql_generation/custom")
    name = Column(String(100), nullable=False, comment="模板名称")
    description = Column(String(500), default="", comment="描述")
    scene_tags = Column(String(200), default="", comment="业务标签（逗号分隔）")
    prompt_template = Column(Text, nullable=False, comment="Prompt正文（支持{{变量}}）")
    output_format = Column(String(20), default="structured_json", comment="输出格式")
    is_default = Column(Boolean, default=False, comment="是否该场景默认")
    is_builtin = Column(Boolean, default=False, comment="是否系统内置（不可编辑/删除）")
    sort_order = Column(Integer, default=0, comment="排序值")
    created_by = Column(BigInteger, default=0, comment="创建人ID")


# ---------- 洞察分析 ----------
class InsightTemplate(Base, AuditMixin):
    """洞察配置模板：保存分析项配置，可重复生成报告。"""
    __tablename__ = "insight_template"
    __table_args__ = {"comment": "洞察配置模板表"}
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    name = Column(String(200), nullable=False, comment="模板名称")
    description = Column(Text, default="", comment="描述")
    purpose = Column(Text, default="", comment="分析目的（原始自然语言）")
    datasource_id = Column(BigInteger, nullable=False, comment="数据源ID")
    config = Column(JSON, default=dict, comment="InsightConfig完整配置（分析项列表）")
    prompt_template_id = Column(BigInteger, nullable=True, comment="AI解读使用的Prompt模板ID")
    created_by = Column(BigInteger, default=0, comment="创建人ID")


class InsightReport(Base, AuditMixin):
    """洞察生成的报告：关联模板+配置快照，写入report表统一管理。"""
    __tablename__ = "insight_report"
    __table_args__ = {"comment": "洞察报告表"}
    workspace_id = Column(BigInteger, nullable=False, comment="工作空间ID")
    template_id = Column(BigInteger, nullable=True, comment="来源模板ID")
    report_id = Column(BigInteger, nullable=True, comment="关联report表ID（统一报告中心）")
    name = Column(String(200), default="", comment="报告名称")
    config_snapshot = Column(JSON, default=dict, comment="生成时的配置快照")
    params = Column(JSON, default=dict, comment="生成时的参数（时间范围等）")
    status = Column(String(20), default="generating", comment="状态：generating/success/failed")
    created_by = Column(BigInteger, default=0, comment="创建人ID")
