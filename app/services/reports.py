import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.errors import ServiceError
from app.schemas import Report, SourceResponse, VacancyReport
from app.services.analyzer import VacancyAnalyzer
from app.services.source import VacancySource, extract_vacancies

logger = logging.getLogger(__name__)


class ReportStore:
    def __init__(self, path: Path):
        self.path = path

    def read(self) -> Report:
        if not self.path.exists():
            raise ServiceError("Успешного отчёта пока нет", status_code=404)
        try:
            return Report.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ServiceError("Не удалось прочитать сохранённый отчёт", status_code=500) from exc

    def save(self, report: Report):
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as file:
                temporary = file.name
                file.write(report.model_dump_json(indent=2, exclude_none=True))
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise ServiceError("Не удалось сохранить отчёт", status_code=500) from exc
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


class ReportService:
    def __init__(self, settings: Settings, source: VacancySource, analyzer: VacancyAnalyzer, store: ReportStore):
        self.settings = settings
        self.source = source
        self.analyzer = analyzer
        self.store = store
        self._run_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._status = {"state": "idle", "started_at": None, "finished_at": None, "processed": 0, "error": None}

    def status(self) -> dict:
        with self._status_lock:
            return self._status.copy()

    def _update(self, **kwargs):
        with self._status_lock:
            self._status.update(kwargs)

    def run(self, payload: SourceResponse | None = None, source_label: str = "local-json") -> Report:
        if not self._run_lock.acquire(blocking=False):
            raise ServiceError("Другой анализ уже выполняется", status_code=409)
        self._update(state="running", started_at=datetime.now(timezone.utc), finished_at=None, processed=0, error=None)
        logger.info("Начат запуск анализа: источник=%s", self.settings.source_url if payload is None else source_label)
        try:
            vacancies = self.source.fetch() if payload is None else extract_vacancies(payload)
            # Одна запись на ID. При повторе внутри входа используется последняя версия.
            unique = {str(vacancy["id"]): vacancy for vacancy in vacancies}
            logger.info("Получено вакансий=%s, уникальных=%s", len(vacancies), len(unique))
            results = []
            for identifier, vacancy in unique.items():
                analysis = self.analyzer.analyze(vacancy)
                url = vacancy.get("vac_url") or vacancy.get("url")
                results.append(VacancyReport(
                    id=identifier,
                    url=url if isinstance(url, str) and url.startswith(("https://", "http://")) else None,
                    issues=analysis.issues,
                ))
                self._update(processed=len(results))
            report = Report(
                created_at=datetime.now(timezone.utc),
                source=self.settings.source_url if payload is None else source_label,
                model=self.settings.gigachat_model,
                total=len(results),
                vacancies=results,
            )
            # Перезаписываем текущий отчёт только после полного успешного анализа.
            self.store.save(report)
            self._update(state="success", finished_at=datetime.now(timezone.utc))
            logger.info("Запуск завершён: вакансий=%s, отчёт=%s", report.total, self.store.path)
            return report
        except ServiceError as exc:
            logger.error("Запуск завершился ошибкой: %s", exc)
            self._update(state="error", error=str(exc), finished_at=datetime.now(timezone.utc))
            raise
        except Exception as exc:
            logger.error("Внутренняя ошибка обработки: %s", type(exc).__name__)
            self._update(state="error", error="Внутренняя ошибка обработки", finished_at=datetime.now(timezone.utc))
            raise ServiceError("Внутренняя ошибка обработки; предыдущий отчёт сохранён", status_code=500) from exc
        finally:
            self._run_lock.release()

    def scheduled_run(self):
        try:
            self.run()
        except ServiceError as exc:
            logger.error("Запуск по расписанию завершился ошибкой: %s", exc)
