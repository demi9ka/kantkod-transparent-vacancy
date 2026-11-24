import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.dependencies import get_report_service
from app.errors import ServiceError
from app.main import create_app
from app.schemas import Analysis, SourceResponse
from app.services.analyzer import VacancyAnalyzer, normalize_vacancy, validate_analysis
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
    assert validate_analysis(json.dumps({"issues": [issue(quote="Пятидневка")]}), vacancy).issues == []
    assert validate_analysis(json.dumps({"issues": [issue(type="contradiction")]}), vacancy).issues == []
    assert validate_analysis(json.dumps({"issues": [issue(source_field="invented")]}), vacancy).issues == []
    result = validate_analysis(json.dumps({"issues": [issue(
        type="contradiction", second_field="requirement.qualification", second_quote="09:00–18:00"
    )]}), vacancy)
    assert result.issues[0].second_quote == "09:00–18:00"


def test_numeric_and_address_evidence():
    vacancy = {"salary_min": 35000, "addresses": {"address": [{"location": "г. Гусев"}]}}
    result = validate_analysis(json.dumps({"issues": [issue(
        source_field="addresses", quote="г. Гусев", topic="workplace"
    )]}), vacancy)
    assert len(result.issues) == 1
    validate_analysis(json.dumps({"issues": [issue(source_field="salary_min", quote="35000", topic="salary")]}), vacancy)


def test_analyzer_keeps_valid_issues_and_logs_invalid_ones(caplog):
    client = Mock()
    client.complete.return_value = json.dumps({"issues": [
        {"bad": True}, issue(), issue(quote="Выдуманная цитата"),
        issue(type="contradiction"), issue(source_field="invented"),
    ]})
    analyzer = VacancyAnalyzer(client, "prompt")
    result = analyzer.analyze({"id": "VAC-1", "schedule": "Гибкий график"})
    assert len(result.issues) == 1
    assert result.issues[0].quote == "Гибкий график"
    assert client.complete.call_count == 1
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 4
    assert all("VAC-1" in r.message and "отброшено" in r.message for r in warnings)
    assert "Цитата" in warnings[1].message


@pytest.mark.parametrize("content", [
    'not-json', '{"issues":', '[]', 'null', '{"issues":null}', '{}',
    '{"issues":[],"extra":true}', '{"issues": [{"bad": true}]}',
])
def test_bad_model_output_logged_and_skipped(content, caplog):
    client = Mock()
    client.complete.return_value = content
    assert VacancyAnalyzer(client, "prompt").analyze({"id": "VAC-2"}).issues == []
    assert "VAC-2" in caplog.text
    assert "WARNING" in caplog.text
    client.complete.assert_called_once()


def test_duplicates_and_excess_issues_are_skipped(caplog):
    vacancy = {"id": "VAC-3", "duty": "; ".join(f"Задача {n}" for n in range(8))}
    issues = [issue(source_field="duty", topic="duties", quote=f"Задача {n}") for n in range(8)]
    result = validate_analysis(json.dumps({"issues": [{"bad": True}, issues[0], *issues]}), vacancy)
    assert [i.quote for i in result.issues] == [f"Задача {n}" for n in range(4)]
    assert "Повтор замечания" in caplog.text
    assert "Превышен лимит" in caplog.text


def test_validation_errors_do_not_stop_report_processing(config, caplog):
    client = Mock()
    client.complete.side_effect = [
        'invalid json',
        json.dumps({"issues": [{"bad": True}, issue()]}),
        '{"issues":[]}',
    ]
    service = ReportService(
        config, Mock(), VacancyAnalyzer(client, "prompt"), ReportStore(config.report_path)
    )
    report = service.run(payload(
        {"id": "1"}, {"id": "2", "schedule": "Гибкий график"}, {"id": "3"}
    ))
    assert report.total == 3
    assert [len(v.issues) for v in report.vacancies] == [0, 1, 0]
    assert service.status()["state"] == "success"
    assert service.store.read() == report
    assert client.complete.call_count == 3


def test_new_prompt_receives_flat_fields_and_original_text():
    vacancy = {
        "id": "FLAT-1", "job-name": "Оператор", "salary_min": 35000,
        "requirement": {"experience": 0, "qualification": "Обучение предоставляется."},
        "addresses": {"address": [{"location": "г. Гусев"}, {"location": "г. Советск"}]},
    }
    normalized = normalize_vacancy(vacancy)
    assert normalized["requirement.experience"] == "0"
    assert normalized["requirement.qualification"] == "Обучение предоставляется."
    assert normalized["salary_min"] == "35000"
    assert normalized["schedule"] is None
    assert normalized["addresses"] == "г. Гусев\nг. Советск"
    assert "id" not in normalized
    assert normalize_vacancy(normalized) == normalized
    client = Mock()
    client.complete.return_value = json.dumps({"issues": [issue(
        topic="requirements", source_field="requirement.qualification", quote="Обучение предоставляется."
    )]})
    assert len(VacancyAnalyzer(client, "новый промпт").analyze(vacancy).issues) == 1
    client.complete.assert_called_once_with("новый промпт", normalized)


def test_shortened_and_old_address_field_names_are_rejected():
    vacancy = {"id": "FLAT-2", "requirement": {"qualification": "Текст"}, "addresses": {"address": [{"location": "Гусев"}]}}
    for field, quote in [("qualification", "Текст"), ("addresses.address[0].location", "Гусев")]:
        assert validate_analysis(json.dumps({"issues": [issue(source_field=field, quote=quote)]}), vacancy).issues == []


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
