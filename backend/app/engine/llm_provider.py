"""模型解析：从「模型配置」表（ModelConfig）解析当前工作空间生效的 LLM / Embedding 客户端。
问数/向量化统一走这里，保证前端配置的模型真正生效。
无配置时返回 None → 调用方走 mock 演示模式。"""
from typing import Any

from sqlalchemy.orm import Session

from ..llm import LLMClient
from ..models import ModelConfig
from ..security import aes_decrypt


def _pick(db: Session, workspace_id: int, scene: str) -> ModelConfig | None:
    m = (db.query(ModelConfig)
         .filter(ModelConfig.workspace_id == workspace_id,
                 ModelConfig.scene == scene,
                 ModelConfig.is_default.is_(True))
         .order_by(ModelConfig.id).first())
    if not m:
        m = (db.query(ModelConfig)
             .filter(ModelConfig.workspace_id == workspace_id,
                     ModelConfig.scene == scene)
             .order_by(ModelConfig.id).first())
    return m


def resolve_llm_client(db: Session, workspace_id: int, scene: str = "sql") -> LLMClient | None:
    """返回可用的 LLMClient；无有效配置（无 key）返回 None。"""
    m = _pick(db, workspace_id, scene)
    if not m or not m.api_key_enc:
        return None
    return _build(m)


def resolve_llm_client_by_id(db: Session, model_id: int, workspace_id: int) -> LLMClient | None:
    """按指定模型 ID 加载（前端 chat 选择模型时使用）；校验归属工作区，无 key 返回 None。"""
    m = (db.query(ModelConfig)
         .filter(ModelConfig.id == model_id,
                 ModelConfig.workspace_id == workspace_id)
         .first())
    if not m or not m.api_key_enc:
        return None
    return _build(m)


def _build(m: ModelConfig) -> LLMClient:
    return LLMClient(base_url=m.base_url,
                     api_key=aes_decrypt(m.api_key_enc),
                     model=m.model_name,
                     embedding_model=m.embedding_model,
                     temperature=m.temperature,
                     max_tokens=m.max_tokens)


def resolve_embed_client(db: Session, workspace_id: int) -> LLMClient | None:
    """Embedding 客户端（scene=embed 优先，回退 sql 场景配置）。"""
    m = _pick(db, workspace_id, "embed") or _pick(db, workspace_id, "sql")
    if not m or not m.api_key_enc:
        return None
    return LLMClient(base_url=m.base_url,
                     api_key=aes_decrypt(m.api_key_enc),
                     model=m.model_name,
                     embedding_model=m.embedding_model,
                     temperature=m.temperature,
                     max_tokens=m.max_tokens)
