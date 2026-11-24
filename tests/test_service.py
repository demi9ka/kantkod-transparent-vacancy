import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.dependencies import get_report_service
from app.errors import ServiceError
from app.main import create_app
from app.schemas import Analysis, SourceResponse
from app.services.analyzer import VacancyAnalyzer, validate_analysis
from app.services.reports import ReportService, ReportStore
from app.services.source import VacancySource


@pytest.fixture
def config(tmp_path):
    return Settings(_env_file=None, report_path=tmp_path / "latest.json", schedule_enabled=False)


def payload(*vacancies):
    return SourceResponse.model_validate({"results": {"vacancies": [{"vacancy": v} for v in vacancies]}})


def issue(**kwargs):
    return {
        "type": "ambiguous", "topic": "schedule", "source_field": "schedule",
        "quote": "Гибкий график", "problem": "Неясно время работы.",
        "recommendation": "Уточнить дни и часы работы.", **kwargs,
    }


def test_quotes_and_contradiction_validation():
    vacancy = {"schedule": "Гибкий график", "requirement": {"qualification": "09:00–18:00"}}
    result = validate_analysis(json.dumps({"issues": [issue()]}), vacancy)
    assert len(result.issues) == 1
    with pytest.raises(ValueError):
        validate_analysis(json.dumps({"issues": [issue(quote="Пятидневка")]}), vacancy)
    with pytest.raises(ValueError):
        validate_analysis(json.dumps({"issues": [issue(type="contradiction")]}), vacancy)
    with pytest.raises(ValueError):
        validate_analysis(json.dumps({"issues": [issue(source_field="invented")]}), vacancy)
    result = validate_analysis(json.dumps({"issues": [issue(
        type="contradiction", second_field="requirement.qualification", second_quote="09:00–18:00"
    )]}), vacancy)
    assert result.issues[0].second_quote == "09:00–18:00"


def test_numeric_and_address_evidence():
    vacancy = {"salary_min": 35000, "addresses": {"address": [{"location": "г. Гусев"}]}}
    for field in ("addresses.address[].location", "addresses.address[0].location"):
        result = validate_analysis(json.dumps({"issues": [issue(
            source_field=field, quote="г. Гусев", topic="workplace"
        )]}), vacancy)
        assert len(result.issues) == 1
    validate_analysis(json.dumps({"issues": [issue(source_field="salary_min", quote="35000", topic="salary")]}), vacancy)


def test_analyzer_retries_invalid_output():
    client = Mock()
    client.complete.side_effect = ['{"issues":[{"bad":true}]}', '{"issues":[]}']
    analyzer = VacancyAnalyzer(client, "prompt")
    assert analyzer.analyze({"id": "1"}).issues == []
    assert client.complete.call_count == 2
    assert client.complete.call_args.kwargs["correction"] is True


def test_bad_model_output_never_becomes_empty_success():
    client = Mock()
    client.complete.return_value = '{"issues": [{"bad": true}]}'
    with pytest.raises(ServiceError):
        VacancyAnalyzer(client, "prompt").analyze({"id": "1"})


def test_deduplication_and_previous_report_preserved(config):
    analyzer = Mock()
    analyzer.analyze.return_value = Analysis(issues=[])
    source = Mock()
    store = ReportStore(config.report_path)
    service = ReportService(config, source, analyzer, store)
    data = payload({"id": "1", "job-name": "Старая"}, {"id": "1", "job-name": "Новая"})
    for _ in range(2):
        assert service.run(data).total == 1
    assert analyzer.analyze.call_args.args[0]["job-name"] == "Новая"
    previous = config.report_path.read_bytes()
    source.fetch.side_effect = ServiceError("Источник недоступен")
    with pytest.raises(ServiceError):
        service.run()
    assert config.report_path.read_bytes() == previous
    assert service.status()["state"] == "error"
    analyzer.analyze.side_effect = ServiceError("Неверная цитата")
    with pytest.raises(ServiceError):
        service.run(payload({"id": "2"}))
    assert config.report_path.read_bytes() == previous


def test_concurrent_run_rejected(config):
    service = ReportService(config, Mock(), Mock(), ReportStore(config.report_path))
    service._run_lock.acquire()
    try:
        with pytest.raises(ServiceError) as error:
            service.run()
        assert error.value.status_code == 409
    finally:
        service._run_lock.release()


def response(vacancies):
    result = Mock()
    result.json.return_value = payload(*vacancies).model_dump()
    return result


def test_source_reads_two_pages_and_honors_limit(config):
    config.max_vacancies = 3
    config.page_size = 2
    source = VacancySource(config)
    source.session = Mock()
    source.session.get.side_effect = [response([{"id": "1"}, {"id": "2"}]), response([{"id": "3"}, {"id": "4"}])]
    assert [v["id"] for v in source.fetch()] == ["1", "2", "3"]
    assert [c.kwargs["params"]["offset"] for c in source.session.get.call_args_list] == [0, 1]


def test_source_repeated_page_fails(config):
    config.max_vacancies = 4
    config.page_size = 2
    source = VacancySource(config)
    source.session = Mock()
    source.session.get.return_value = response([{"id": "1"}, {"id": "2"}])
    with pytest.raises(ServiceError, match="повторяет"):
        source.fetch()


def test_api_local_mode_analyzes_all_sample_vacancies(config):
    config.max_vacancies = 1  # Ограничение портала не распространяется на локальный JSON.
    analyzer = Mock()
    analyzer.analyze.return_value = Analysis(issues=[])
    source = Mock()
    service = ReportService(config, source, analyzer, ReportStore(config.report_path))
    app = create_app(config)
    app.dependency_overrides[get_report_service] = lambda: service
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/report").status_code == 404
        data = json.loads((ROOT / "kantkod_sample_vacancies (1).json").read_text())
        result = client.post("/analyze", json=data)
        assert result.status_code == 200
        assert result.json()["total"] == 10
        assert len(result.json()["vacancies"]) == 10
        source.fetch.assert_not_called()
        assert client.get("/report").json() == result.json()
        assert client.post("/analyze", json={}).status_code == 422
        assert client.post("/analyze", json={"results": {"vacancies": [{"vacancy": {}}]}}).status_code == 422
        assert client.get("/openapi.json").status_code == 200


def test_missing_key_returns_503(config):
    from pydantic import SecretStr
    config.gigachat_auth_key = SecretStr("")
    with TestClient(create_app(config)) as client:
        response = client.get("/models")
        assert response.status_code == 503
        assert "GIGACHAT_AUTH_KEY" in response.json()["detail"]


def test_scheduler_registered(config):
    from unittest.mock import patch
    config.schedule_enabled = True
    with patch("app.main.BackgroundScheduler") as scheduler:
        with TestClient(create_app(config)):
            scheduler.return_value.add_job.assert_called_once()
            scheduler.return_value.start.assert_called_once()
        scheduler.return_value.shutdown.assert_called_once()
