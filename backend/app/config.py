"""应用配置：pydantic-settings 加载 .env / 环境变量。"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 元数据库（MySQL AI_Infra，见需求 5.4）
    db_host: str = "localhost"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = "1234qwer!@#$"
    db_name: str = "AI_Infra"
    database_url: str = ""  # 显式指定时优先；为空则按 MySQL 字段拼接

    # 安全
    secret_key: str = "dev-secret-change-me"
    jwt_expire_minutes: int = 1440

    # LLM（OpenAI 兼容）
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    embed_model: str = "text-embedding-3-small"

    # 向量库
    vector_backend: str = "builtin"  # builtin | milvus | chroma
    vector_collection_prefix: str = "ai_sql_bot"

    # 执行与安全
    cors_origins: str = "*"
    query_timeout_seconds: int = 30
    query_max_rows: int = 1000
    sql_correct_retries: int = 2

    # ===== 能力补建配置（C1/C2/C3/C4/C6/C7/C8/C11/C14）=====
    # C1 缓存
    cache_enabled: bool = True
    cache_ttl_result_sec: int = 900          # 结果缓存 TTL
    cache_ttl_gen_sec: int = 604800          # 生成缓存 TTL（7 天）
    cache_similarity: float = 0.92           # 相似问句命中阈值
    cache_max_entries_per_ws: int = 5000     # 单 workspace 缓存条目上限

    # C2 成本门槛
    cost_guard_enabled: bool = True
    cost_guard_explain_timeout_ms: int = 2000
    cost_guard_mysql_rows_soft: int = 1_000_000
    cost_guard_mysql_rows_hard: int = 10_000_000
    cost_guard_pg_cost_soft: float = 50_000
    cost_guard_pg_cost_hard: float = 500_000

    # C6 超时与限流
    rate_limit_enabled: bool = True
    rate_limit_qps: float = 2.0
    rate_limit_burst: int = 5
    rate_limit_max_concurrent: int = 5
    rate_limit_queue_timeout_s: float = 10.0
    query_timeout_ms: int = 30000

    # C7 样例值采集
    schema_sync_collect_samples: bool = True
    schema_sync_sample_per_column: int = 5
    schema_sync_sample_min_rows: int = 1000
    schema_sync_sample_timeout_s: float = 3.0

    # C8 血缘采集
    lineage_collect_enabled: bool = True
    lineage_ddl_interval_min: int = 360
    lineage_mine_interval_min: int = 720
    lineage_max_depth: int = 3
    lineage_parse_timeout_s: float = 60.0

    # C4 评测
    eval_mock_execute: bool = True
    eval_llm_judge_correct: bool = False

    # C14 场景路由
    scene_auto_route: bool = True
    scene_llm_fallback: bool = True

    # C11 多候选
    multi_candidate_enabled: bool = True

    # ===== 多查询 V2.0（两阶段协同编排）=====
    multi_max_concurrent: int = 4           # 子查询并发上限（同时受 rate_limit 约束）
    multi_max_subqueries: int = 5           # 拆解子查询数量上限（超限合并或提示）
    multi_sub_timeout_s: float = 60.0       # 单子查询总超时（生成+执行+修正）
    multi_sub_gen_retry: int = 1            # SQL 生成失败重试次数
    multi_sub_exec_retry: int = 1           # 执行失败 LLM 自动修正次数
    multi_confirm_timeout_s: float = 60.0   # Phase A 等待用户确认超时（超时按原始清单自动执行）
    multi_budget_llm_calls: int = 0         # 单次多查询 LLM 调用预算上限（0=不限）

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        from urllib.parse import quote_plus
        user = quote_plus(self.db_user)
        pwd = quote_plus(self.db_password)
        return (
            f"mysql+pymysql://{user}:{pwd}@{self.db_host}:{self.db_port}/{self.db_name}"
            "?charset=utf8mb4"
        )

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
