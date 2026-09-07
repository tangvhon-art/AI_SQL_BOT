"""模型配置管理（LLM / Embedding，OpenAI 兼容）。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..llm import LLMClient, LLMError
from ..models import ModelConfig
from ..security import aes_decrypt, aes_encrypt
from .deps import get_current_user

router = APIRouter(prefix="/models", tags=["models"])


class ModelIn(BaseModel):
    name: str
    provider: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model_name: str = ""
    embedding_model: str = ""
    temperature: float = 0.1
    top_p: float = 0.9
    max_tokens: int = 2048
    scene: str = "sql"
    is_default: bool = False


def _out(m: ModelConfig) -> dict:
    return {"id": m.id, "name": m.name, "provider": m.provider, "base_url": m.base_url,
            "model_name": m.model_name, "embedding_model": m.embedding_model,
            "temperature": m.temperature, "top_p": m.top_p, "max_tokens": m.max_tokens,
            "scene": m.scene, "is_default": m.is_default, "has_key": bool(m.api_key_enc)}


@router.get("")
def list_models(db: Session = Depends(get_db), user=Depends(get_current_user)):
    models = (db.query(ModelConfig)
              .filter(ModelConfig.workspace_id == user.workspace_id).all())
    return [_out(m) for m in models]


@router.post("")
def create_model(body: ModelIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    m = ModelConfig(workspace_id=user.workspace_id, name=body.name, provider=body.provider,
                    base_url=body.base_url, api_key_enc=aes_encrypt(body.api_key),
                    model_name=body.model_name, embedding_model=body.embedding_model,
                    temperature=body.temperature, top_p=body.top_p,
                    max_tokens=body.max_tokens, scene=body.scene, is_default=body.is_default)
    db.add(m)
    db.commit()
    db.refresh(m)
    return _out(m)


@router.put("/{model_id}")
def update_model(model_id: int, body: ModelIn, db: Session = Depends(get_db),
                 user=Depends(get_current_user)):
    m = db.query(ModelConfig).get(model_id)
    if not m:
        raise HTTPException(404, "模型不存在")
    for f in ("name", "provider", "base_url", "model_name", "embedding_model",
              "temperature", "top_p", "max_tokens", "scene", "is_default"):
        setattr(m, f, getattr(body, f))
    if body.api_key:
        m.api_key_enc = aes_encrypt(body.api_key)
    db.commit()
    return _out(m)


@router.delete("/{model_id}")
def delete_model(model_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    m = db.query(ModelConfig).get(model_id)
    if m:
        m.is_deleted = True
        db.commit()
    return {"ok": True}


@router.post("/{model_id}/test")
def test_model(model_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    m = db.query(ModelConfig).get(model_id)
    if not m:
        raise HTTPException(404, "模型不存在")
    client = LLMClient(base_url=m.base_url, api_key=aes_decrypt(m.api_key_enc),
                       model=m.model_name, temperature=m.temperature)
    try:
        reply = client.chat([{"role": "user", "content": "回复 OK"}], max_tokens=8, thinking=False)
        return {"ok": True, "reply": reply[:50]}
    except LLMError as exc:
        return {"ok": False, "message": str(exc)}
