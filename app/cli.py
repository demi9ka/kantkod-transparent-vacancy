import argparse
import logging
import sys
from pathlib import Path

from pydantic import ValidationError

from app.config import Settings
from app.errors import ServiceError
from app.main import build_service
from app.logging_config import configure_logging
from app.schemas import SourceResponse


def main() -> int:
    parser = argparse.ArgumentParser(description="Анализ вакансий с помощью GigaChat")
    parser.add_argument("--input", type=Path, help="Локальный ответ API; без параметра данные загружаются с портала")
    parser.add_argument("--output", type=Path, help="Путь к JSON-отчёту")
    args = parser.parse_args()
    service = client = None
    try:
        config = Settings()
        configure_logging(config)
        if args.output:
            config.report_path = args.output.resolve()
        payload = SourceResponse.model_validate_json(args.input.read_text(encoding="utf-8")) if args.input else None
        service, client = build_service(config)
        report = service.run(payload, source_label=args.input.name if args.input else "local-json")
        print(f"Обработано вакансий: {report.total}. Отчёт: {config.report_path}")
        print("Статусы: " + ", ".join(f"{status}={sum(v.status == status for v in report.vacancies)}" for status in ("ok", "partial", "error")))
        return 0
    except (ServiceError, OSError, ValidationError) as exc:
        message = "Некорректная конфигурация или формат входного JSON" if isinstance(exc, ValidationError) else str(exc)
        logging.getLogger(__name__).error("Запуск CLI завершился ошибкой: %s", message)
        print(f"Ошибка: {message}", file=sys.stderr)
        return 1
    finally:
        if client:
            client.close()
        if service:
            service.source.close()


if __name__ == "__main__":
    raise SystemExit(main())
