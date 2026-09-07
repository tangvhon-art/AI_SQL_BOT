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
