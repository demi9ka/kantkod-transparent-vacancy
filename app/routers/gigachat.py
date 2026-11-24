from fastapi import APIRouter

from app.dependencies import GigaChatDep

router = APIRouter(tags=["GigaChat"])


@router.get("/models", summary="Проверить подключение и получить модели GigaChat")
def models(client: GigaChatDep):
    return client.models()
