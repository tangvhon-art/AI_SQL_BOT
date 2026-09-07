"""SQLAlchemy ORM 模型：对应需求/设计第 8 章数据模型。
对齐团队数据库设计规范：统一审计字段、小写下划线命名、全字段 COMMENT、InnoDB/utf8mb4、
逻辑删除（is_deleted + delete_time）、索引命名约定（idx_/uk_，见 database.Base 的 naming_convention）。"""
from datetime import datetime

from sqlalchemy import (JSON, BigInteger, Boolean, Column, DateTime, Float,
                        Integer, SmallInteger, String, Text,
                        UniqueConstraint, text)
from sqlalchemy.dialects import mysql

from .database import Base

# 主键：MySQL 生成 BIGINT UNSIGNED 自增；SQLite 降级为 INTEGER
BIGINT_UID = BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


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
