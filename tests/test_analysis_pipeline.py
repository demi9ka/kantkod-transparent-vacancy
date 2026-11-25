import json
from unittest.mock import Mock, call

import pytest

from app.errors import ModelResponseError, ServiceError
from app.schemas import AnalysisResult, Issue, MAX_ISSUES
from app.services.analyzer import VacancyAnalyzer, merge_results, validate_analysis
from app.services.quotes import match_quote, protected_values
from app.services.rules import address_known, deterministic_issues, empty_field, remote_work
from app.services.vacancy import normalize_vacancy


def vacancy(**updates):
    return {
        "id": "V1", "job-name": "Оператор", "employment": "Полная занятость",
        "salary_min": 65000, "salary_max": 65000, "requirements": "Знание ПК",
        "addresses": "г. Гусев, ул. Ленина, д. 5", "schedule": "Гибкий график",
        **updates,
    }


def issue(**updates):
    return {
        "type": "ambiguous", "topic": "schedule", "source_field": "schedule",
        "quote": "Гибкий график", "problem": "Неясно время работы.",
        "recommendation": "Уточнить время работы.", **updates,
    }


def analyze_items(items, data=None, repair=None):
    return validate_analysis(json.dumps({"issues": items}, ensure_ascii=False), data or vacancy(), repair=repair)


@pytest.mark.parametrize("bad", ["", "invalid", "[]", '{"issues":null}', '{"other":[]}'])
def test_whole_response_retried_with_exact_backoff(monkeypatch, bad):
    sleep = Mock()
    monkeypatch.setattr("app.services.analyzer.time.sleep", sleep)
    client = Mock()
    client.complete.side_effect = [bad, bad, bad, '{"issues":[]}']
    result = VacancyAnalyzer(client, "prompt").analyze(vacancy())
    assert result.status == "ok" and result.issues == []
    assert client.complete.call_count == 4
    assert sleep.call_args_list == [call(1), call(2), call(4)]
    client.repair_issue.assert_not_called()


def test_exhausted_attempts_return_error_with_reason(monkeypatch):
    monkeypatch.setattr("app.services.analyzer.time.sleep", Mock())
    client = Mock()
    client.complete.side_effect = ModelResponseError("Пустой content")
    result = VacancyAnalyzer(client, "prompt").analyze(vacancy())
    assert result.status == "error" and result.error == "Пустой content"
    assert result.dropped_count == 0
    assert client.complete.call_count == 4


def test_transport_failure_does_not_trigger_content_retry(monkeypatch):
    sleep = Mock()
    monkeypatch.setattr("app.services.analyzer.time.sleep", sleep)
    client = Mock()
    client.complete.side_effect = ServiceError("HTTP 403")
    with pytest.raises(ServiceError):
        VacancyAnalyzer(client, "prompt").analyze(vacancy())
    assert client.complete.call_count == 1
    sleep.assert_not_called()


def test_one_invalid_item_marks_partial_without_repeating_vacancy():
    client = Mock()
    client.complete.return_value = json.dumps({"issues": [{"bad": True}, issue()]})
    result = VacancyAnalyzer(client, "prompt").analyze(vacancy())
    assert result.status == "partial" and len(result.issues) == 1
    assert result.dropped_count == len(result.dropped_reasons) == 1
    client.complete.assert_called_once()


def test_all_items_dropped_are_partial_and_logged(caplog):
    result = analyze_items([{"bad": True}])
    assert result.status == "partial" and result.issues == []
    assert result.dropped_count == 1
    assert "все замечания" in caplog.text


def test_targeted_repair_is_one_separate_request():
    client = Mock()
    original = issue(quote="", problem="Неясны правила гибкости графика.")
    client.complete.return_value = json.dumps({"issues": [original]})
    client.repair_issue.return_value = json.dumps({"issues": [issue()]})
    result = VacancyAnalyzer(client, "prompt").analyze(vacancy())
    assert result.status == "ok" and result.issues[0].quote == "Гибкий график"
    client.complete.assert_called_once()
    client.repair_issue.assert_called_once()
    assert client.repair_issue.call_args.args[2] == original


def test_targeted_repair_is_not_repeated_if_bad():
    client = Mock()
    original = issue(quote="", problem="Неясны правила гибкости графика.")
    client.complete.return_value = json.dumps({"issues": [original]})
    client.repair_issue.return_value = 'invalid JSON'
    result = VacancyAnalyzer(client, "prompt").analyze(vacancy())
    assert result.status == "partial"
    assert result.dropped_count == 1
    client.complete.assert_called_once()
    client.repair_issue.assert_called_once()


@pytest.mark.parametrize("field,value,expected", [
    ("salary_min", "0", True), ("salary_max", 0, True),
    ("requirement.experience", "0", False), ("requirement.experience", 0, False),
    ("requirements", "0", False), ("employment", "", True),
])
def test_zero_is_missing_only_for_salary(field, value, expected):
    assert empty_field(field, value) == expected


def test_missing_quote_cleared_only_when_gap_is_real():
    data = vacancy(salary_min=0)
    result = analyze_items([issue(type="missing", topic="salary", source_field="salary_min", quote="0")], data)
    assert result.status == "ok" and result.issues[0].quote == ""
    result = analyze_items([issue(type="missing", topic="salary", source_field="salary_min", quote="65000")])
    assert result.status == "partial" and not result.issues


def test_ambiguous_empty_quote_checks_other_fields_first():
    raw = issue(type="ambiguous", source_field="employment", topic="employment", quote="", problem="Не указан тип занятости.")
    result = analyze_items([raw], vacancy(employment=None, duty="Предлагается полная занятость."))
    assert result.status == "partial" and not result.issues
    result = analyze_items([raw], vacancy(employment=None, schedule=None))
    assert result.status == "ok" and result.issues[0].type == "missing"


def test_schedule_information_elsewhere_is_not_missing():
    raw = issue(quote="", problem="Не указано время работы.")
    result = analyze_items([raw], vacancy(schedule=None, requirement={"qualification": "Работа с 8 до 17."}))
    assert result.status == "partial" and not result.issues


def test_title_in_duties_is_not_missing():
    raw = issue(topic="title", source_field="job-name", quote="", problem="Не указано название должности.")
    result = analyze_items([raw], vacancy(**{"job-name": None, "duty": "Вакансия: бухгалтер"}))
    assert result.status == "partial" and not result.issues


def test_experience_zero_cannot_be_downgraded_to_missing():
    data = vacancy(requirement={"experience": 0})
    raw = issue(topic="requirements", source_field="requirement.experience", quote="", problem="Не указан опыт.")
    assert not analyze_items([raw], data).issues


def test_salary_alias_mapped_only_with_unambiguous_evidence():
    raw = issue(type="missing", topic="salary", source_field="salary", quote="", problem="Не указана максимальная зарплата.")
    result = analyze_items([raw], vacancy(salary_max=None))
    assert result.issues[0].source_field == "salary_max"
    result = analyze_items([{**raw, "problem": "Зарплата не указана."}], vacancy(salary_min=0, salary_max=0))
    assert result.status == "partial" and not result.issues


@pytest.mark.parametrize("quote,source", [
    ("СМЕННЫЙ ГРАФИК", "Сменный график"),
    ("прием документов", "Приём документов"),
    ("oпыт pабoты", "опыт работы"),
    ("работа с документами", "Работа   с\nдокументами"),
    ("Работа с документами", "Работа — с документами"),
    ("Рабочее время 8 часов сменный график", "Рабочее время 8 часов, сменый график"),
    ("Пунктуальность Ответственность", "ПунктуальностьОтветственность"),
    ("Смена 08:00 — 17:00", "Смена 08.00 - 17.00"),
    ("Оклад 65 000 рублей", "Оклад 65000 рублей"),
])
def test_quote_repair_returns_exact_source_substring(quote, source):
    fixed, method = match_quote(quote, source)
    assert fixed in source
    assert method in ("normalized", "fuzzy")
    assert protected_values(fixed) == protected_values(quote)


@pytest.mark.parametrize("quote,source", [
    ("Опыт работы 5 лет", "Опыт работы 3 лет"),
    ("Опыт работы обязателен", "Опыт работы не обязателен"),
    ("Обязательно наличие опыта", "Желательно наличие опыта"),
    ("Работа только в офисе", "Работа в офисе"),
    ("Зарплата 65000 рублей", "Зарплата 6500 рублей"),
    ("5 лет", "15 лет"),
    ("5 градусов", "-5 градусов"),
])
def test_quote_repair_never_changes_numbers_or_conditions(quote, source):
    with pytest.raises(ValueError):
        match_quote(quote, source)


def test_second_quote_also_repaired_and_logged(caplog):
    raw = issue(type="contradiction", second_field="requirement.qualification", second_quote="09:00 - 18:00")
    result = analyze_items([raw], vacancy(requirement={"qualification": "09:00 — 18:00"}))
    assert result.status == "ok"
    assert result.issues[0].second_quote == "09:00 — 18:00"


@pytest.mark.parametrize("updates", [
    {"duty": "Место работы: ул. Ленина, д. 7"},
    {"requirements": "Выходить на работу: проспект Мира, 8"},
    {"requirement": {"qualification": "Ориентир: центральная проходная завода"}},
    {"duty": "Работа полностью дистанционная из любого города"},
    {"duty": "Формат работы: дистанционный"},
    {"duty": "Место работы: ТЦ «Балтика»"},
])
def test_address_missing_suppressed_by_other_fields(updates):
    data = normalize_vacancy(vacancy(addresses="г. Гусев", **updates))
    assert not any(i.topic == "workplace" for i in deterministic_issues(data))
    raw = issue(type="missing", topic="workplace", source_field="addresses", quote="", problem="Не указан точный адрес.")
    assert not analyze_items([raw], data).issues


def test_street_date_is_not_mistaken_for_house_number():
    assert not address_known(normalize_vacancy(vacancy(addresses="ул. 8 Марта")))
    assert address_known(normalize_vacancy(vacancy(addresses="ул. 8 Марта, 12")))


@pytest.mark.parametrize("hint", ["полная занятость", "неполный день", "сменный график", "вахта", "0.5 ставки"])
def test_employment_hints_anywhere_prevent_false_missing(hint):
    data = normalize_vacancy(vacancy(employment=None, duty=hint))
    assert not any(i.topic == "employment" for i in deterministic_issues(data))


def test_delivery_and_watchman_are_not_employment_types():
    data = normalize_vacancy(vacancy(employment=None, schedule=None, duty="Организация доставки товаров", **{"job-name": "Вахтер"}))
    assert any(i.topic == "employment" for i in deterministic_issues(data))


def test_salary_and_requirements_in_text_prevent_false_missing():
    data = normalize_vacancy(vacancy(
        salary_min=0, salary_max=0, requirements=None,
        duty="Требования: знание компьютера. Зарплата 65000 рублей.",
    ))
    assert not deterministic_issues(data)


def test_deterministic_rules_partial_salary_and_experience_zero():
    data = normalize_vacancy(vacancy(employment=None, schedule=None, salary_min=0, requirements=None, addresses="Гусев", requirement={"experience": 0}))
    result = deterministic_issues(data)
    assert {i.topic for i in result} == {"employment", "salary", "requirements", "workplace"}
    salary = next(i for i in result if i.topic == "salary")
    assert salary.source_field == "salary_min" and "неполная" in salary.problem
    assert all(i.type == "missing" and i.quote == "" for i in result)


def test_unsupported_employer_and_fabricated_examples():
    raw = issue(problem="Не указано название работодателя.")
    assert not analyze_items([raw]).issues
    raw = issue(recommendation="Уточнить должность, например, 'Уборщик кухни'.")
    assert analyze_items([raw]).issues[0].recommendation == "Уточнить должность."


def test_limit_prioritizes_rules_and_contradictions():
    rules = deterministic_issues(normalize_vacancy(vacancy(employment=None, salary_min=0, salary_max=0, requirements=None, addresses=None, schedule=None)))
    ai = [Issue.model_validate(issue(source_field=f, topic=t)) for f, t in [
        ("job-name", "title"), ("duty", "duties"), ("schedule", "schedule"),
    ]]
    contradiction = Issue.model_validate(issue(type="contradiction", second_field="duty", second_quote="В офисе"))
    result = merge_results(AnalysisResult(issues=[contradiction, *ai]), rules, "V1")
    assert len(result.issues) == MAX_ISSUES == 6
    assert result.issues[:4] == rules
    assert result.issues[4].type == "contradiction"
    assert result.status == "partial" and result.dropped_count == 2


def test_rule_and_llm_duplicate_is_single_issue():
    data = vacancy(employment=None, schedule=None)
    raw = issue(type="missing", topic="employment", source_field="employment", quote="", problem="Не указан тип занятости.")
    client = Mock()
    client.complete.return_value = json.dumps({"issues": [raw]})
    result = VacancyAnalyzer(client, "prompt").analyze(data)
    assert len(result.issues) == 1
    assert result.dropped_count == 1 and result.status == "partial"
