import json
import logging
from logging.handlers import RotatingFileHandler
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.logging_config import configure_logging
from app.services.analyzer import VacancyAnalyzer


@pytest.fixture
def log_settings(tmp_path):
    app_logger = logging.getLogger("app")
    previous_handlers = app_logger.handlers[:]
    previous_level = app_logger.level
    app_logger.handlers = []
    config = Settings(
        _env_file=None, log_file=tmp_path / "logs/app.log",
        report_path=tmp_path / "report.json",
        log_level="DEBUG", log_console_level="INFO", gigachat_auth_key=SecretStr("test-secret-key"),
    )
    try:
        yield config
    finally:
        for handler in app_logger.handlers[:]:
            app_logger.removeHandler(handler)
            handler.close()
        app_logger.handlers = previous_handlers
        app_logger.setLevel(previous_level)


def test_file_contains_source_answer_and_mismatch(log_settings, capsys):
    configure_logging(log_settings)
    client = Mock()
    client.complete.return_value = json.dumps({"issues": [{
        "type": "ambiguous", "topic": "schedule", "source_field": "schedule",
        "quote": "Сменный график", "problem": "Неясен график", "recommendation": "Уточнить часы",
    }]}, ensure_ascii=False)
    result = VacancyAnalyzer(client, "Тестовый промпт").analyze({
        "id": "LOG-1", "schedule": "Гибкий график", "requirement": {"experience": 0},
        "employment": "Полная занятость", "salary_min": 50000, "salary_max": 50000,
        "requirements": "Навыки работы с ПК", "addresses": "ул. Учебная, д. 1",
    })
    assert result.issues == []
    log = log_settings.log_file.read_text(encoding="utf-8")
    for value in ("LOG-1", "исходные данные", "данные для ИИ", "ответ ИИ",
                  "Сменный график", "Гибкий график", "поле=schedule", "совпадение=False", "WARNING"):
        assert value in log
    assert "Тестовый промпт" not in log
    assert "системный промпт" not in log
    console = capsys.readouterr().err
    assert "WARNING" in console
    assert "DEBUG" not in console


def test_secrets_redacted_and_setup_is_idempotent(log_settings):
    configure_logging(log_settings)
    configure_logging(log_settings)
    logger = logging.getLogger("app.logging_test")
    logger.debug('Ключ test-secret-key; Bearer bearer-secret; {"access_token": "json-secret"}')
    log = log_settings.log_file.read_text(encoding="utf-8")
    assert log.count("Ключ") == 1
    assert "test-secret-key" not in log
    assert "bearer-secret" not in log
    assert "json-secret" not in log
    assert "[REDACTED]" in log
    handlers = logging.getLogger("app").handlers
    assert len(handlers) == 2
    file_handler = next(h for h in handlers if isinstance(h, RotatingFileHandler))
    assert file_handler.maxBytes == 5 * 1024 * 1024
    assert file_handler.backupCount == 3


def test_info_level_omits_debug_payloads(log_settings):
    log_settings.log_level = "INFO"
    configure_logging(log_settings)
    logger = logging.getLogger("app.logging_test")
    logger.debug("подробные данные")
    logger.info("запуск завершён")
    log = log_settings.log_file.read_text(encoding="utf-8")
    assert "подробные данные" not in log
    assert "запуск завершён" in log


@pytest.mark.parametrize("mode", ["http", "cli"])
def test_http_and_cli_write_debug_file(log_settings, monkeypatch, mode):
    from fastapi.testclient import TestClient
    from app import cli
    from app.main import create_app
    from app.services.gigachat import GigaChatClient

    monkeypatch.setattr(GigaChatClient, "complete", lambda *args: '{"issues":[]}')
    payload = {"results": {"vacancies": [{"vacancy": {"id": "ENTRY-1", "schedule": "Гибкий график"}}]}}
    if mode == "http":
        with TestClient(create_app(log_settings)) as client:
            assert client.post("/analyze", json=payload).status_code == 200
    else:
        source = log_settings.log_file.parent.parent / "input.json"
        source.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(cli, "Settings", lambda: log_settings)
        monkeypatch.setattr("sys.argv", ["app.cli", "--input", str(source)])
        assert cli.main() == 0
    log = log_settings.log_file.read_text(encoding="utf-8")
    assert "ENTRY-1" in log and "DEBUG" in log
    assert "Запуск завершён" in log
