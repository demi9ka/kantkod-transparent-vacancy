from fastapi import APIRouter

from app.dependencies import ReportServiceDep
from app.schemas import Report, SourceResponse

router = APIRouter(tags=["Отчёты"])


@router.post("/analyze", response_model=Report, response_model_exclude_none=True, summary="Анализ локального JSON API")
def analyze(payload: SourceResponse, service: ReportServiceDep):
    return service.run(payload)


@router.post("/runs", response_model=Report, response_model_exclude_none=True, summary="Загрузить вакансии и создать отчёт")
def run(service: ReportServiceDep):
    return service.run()


@router.get("/runs/status", summary="Состояние последнего запуска")
def status(service: ReportServiceDep):
    return service.status()


@router.get("/report", response_model=Report, response_model_exclude_none=True, summary="Последний успешный отчёт")
def report(service: ReportServiceDep):
    return service.store.read()
