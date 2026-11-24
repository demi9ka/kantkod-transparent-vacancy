from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import Settings
from app.errors import ServiceError
from app.logging_config import configure_logging
from app.routers import gigachat, reports
from app.services.analyzer import VacancyAnalyzer
from app.services.gigachat import GigaChatClient
from app.services.reports import ReportService, ReportStore
from app.services.source import VacancySource


def build_service(settings: Settings) -> tuple[ReportService, GigaChatClient]:
    client = GigaChatClient(settings)
    source = VacancySource(settings)
    analyzer = VacancyAnalyzer(client, settings.prompt_path.read_text(encoding="utf-8"))
    return ReportService(settings, source, analyzer, ReportStore(settings.report_path)), client


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings or Settings()
        configure_logging(config)
        service, client = build_service(config)
        app.state.report_service = service
        app.state.gigachat_client = client
        scheduler = BackgroundScheduler(timezone=config.timezone)
        if config.schedule_enabled:
            scheduler.add_job(
                service.scheduled_run,
                CronTrigger.from_crontab(config.schedule_cron, timezone=config.timezone),
                id="vacancy-report", max_instances=1, coalesce=True, misfire_grace_time=300,
            )
            scheduler.start()
        try:
            yield
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=True)
            client.close()
            service.source.close()

    application = FastAPI(
        title="Прозрачная вакансия", version="1.0.0",
        description="Анализ понятности вакансий Калининградской области с помощью GigaChat.",
        lifespan=lifespan,
    )

    @application.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @application.get("/health", tags=["Сервис"], summary="Проверка работоспособности")
    def health():
        return {"status": "ok"}

    application.include_router(reports.router)
    application.include_router(gigachat.router)
    return application


app = create_app()
