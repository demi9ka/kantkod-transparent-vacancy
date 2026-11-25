import json
import logging
import re
import time
from typing import Any, Callable

from pydantic import ValidationError

from app.errors import ModelResponseError, ServiceError
from app.schemas import MAX_ISSUES, AnalysisResult, Issue
from app.services.gigachat import GigaChatClient
from app.services.quotes import match_quote
from app.services.rules import deterministic_issues, empty_field, missing_evidence
from app.services.vacancy import FIELDS, field_values, normalize_vacancy

logger = logging.getLogger(__name__)
ALIASES = {
    "job_name": "job-name", "title": "job-name", "duties": "duty",
    "qualification": "requirement.qualification", "education": "requirement.education",
    "experience": "requirement.experience", "address": "addresses",
    "addresses.address[].location": "addresses", "addresses.address[0].location": "addresses",
}


class RepairNeeded(ValueError):
    pass


def validation_details(error: ValueError) -> str:
    if isinstance(error, ValidationError):
        return str(error.errors(include_input=False, include_url=False))
    return str(error)


def parse_response(content: str) -> list[Any]:
    if not isinstance(content, str) or not content.strip():
        raise ModelResponseError("Ответ модели должен содержать непустой текст JSON")
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise ModelResponseError("Ответ модели содержит невалидный JSON") from exc
    if not isinstance(data, dict) or set(data) != {"issues"} or not isinstance(data["issues"], list):
        raise ModelResponseError('Ожидается корневой объект {"issues": [...]} без дополнительных полей')
    return data["issues"]


def map_field(field: Any, raw: dict, data: dict) -> str:
    if not isinstance(field, str):
        raise ValueError("Имя поля должно быть строкой")
    if field in FIELDS:
        return field
    if field in ALIASES:
        return ALIASES[field]
    if field == "salary":
        description = str(raw.get("problem", "")).lower()
        low = bool(re.search(r"миним|нижн", description))
        high = bool(re.search(r"максим|верхн", description))
        if low and high:
            raise ValueError("Поле salary неоднозначно: замечание относится к обеим границам")
        if low != high:
            return "salary_min" if low else "salary_max"
        quote = str(raw.get("quote", ""))
        matches = [f for f in ("salary_min", "salary_max") if quote and quote == data.get(f)]
        if len(matches) == 1:
            return matches[0]
    raise ValueError(f"Неизвестное или неоднозначное поле {field!r}")


def normalize_issue(raw: Any, data: dict, vacancy_id: str) -> Issue:
    if not isinstance(raw, dict):
        raise ValueError("Замечание должно быть объектом")
    result = raw.copy()
    for name in ("source_field", "second_field"):
        if result.get(name) is None:
            continue
        context = result if name == "source_field" else {**result, "quote": result.get("second_quote")}
        mapped = map_field(result[name], context, data)
        if mapped != result[name]:
            logger.info("Вакансия %s: исправлено %s: %r -> %r", vacancy_id, name, result[name], mapped)
            result[name] = mapped
    description = str(result.get("problem", "")).lower()
    if re.search(r"названи[ея]\s+(?:работодател|компани|организаци)|(?:не\s+указан\w*|нет)\s+работодател|работодатель\s+(?:не\s+указан|неизвестен)|кто\s+работодатель", description):
        raise ValueError("Сведения о работодателе не входят в передаваемые модели поля")
    if result.get("type") == "missing":
        if result.get("quote") != "":
            logger.info("Вакансия %s: missing.quote исправлена: %r -> ''", vacancy_id, result.get("quote"))
            result["quote"] = ""
        evidence = missing_evidence(result, data)
        if evidence != "absent":
            raise ValueError(f"Пропуск не подтверждён всей вакансией: сведения {evidence}")
    if result.get("type") == "ambiguous" and result.get("quote") in (None, ""):
        evidence = missing_evidence(result, data)
        if evidence == "present":
            raise ValueError("Неоднозначность без цитаты: искомые сведения уже есть в вакансии")
        if evidence == "absent" and empty_field(result.get("source_field"), data.get(result.get("source_field"))):
            logger.info("Вакансия %s: ambiguous -> missing: пропуск подтверждён всей вакансией", vacancy_id)
            result.update(type="missing", quote="")
        else:
            raise RepairNeeded("Неоднозначность без цитаты: нужно уточнить замечание по исходным данным")
    recommendation = result.get("recommendation")
    if isinstance(recommendation, str) and re.search(r"\bнапример\b", recommendation, re.I):
        fixed = re.split(r"\bнапример\b", recommendation, maxsplit=1, flags=re.I)[0].rstrip(" ,;:—-(")
        if not fixed:
            raise RepairNeeded("Нужна рекомендация без вымышленных примеров")
        logger.info("Вакансия %s: рекомендация без примера: %r -> %r", vacancy_id, recommendation, fixed)
        result["recommendation"] = fixed.rstrip(".") + "."
    # Цитаты восстанавливаются из источника до проверки строгой схемы.
    for field_key, quote_key in (("source_field", "quote"), ("second_field", "second_quote")):
        quote = result.get(quote_key)
        field = result.get(field_key)
        if not isinstance(quote, str) or not quote or field not in FIELDS:
            continue
        source = data.get(field) or ""
        logger.debug("Вакансия %s: поле=%s; цитата ИИ=%r; исходное значение=%r; совпадение=%s", vacancy_id, field, quote, source, quote in source)
        corrected, method = match_quote(quote, source)
        logger.debug("Вакансия %s: поле=%s; цитата ИИ=%r; исходное значение=%r; метод=%s", vacancy_id, field, quote, source, method)
        if corrected != quote:
            logger.info("Вакансия %s: исправлена %s: исходная=%r; итоговая=%r; метод=%s", vacancy_id, quote_key, quote, corrected, method)
            result[quote_key] = corrected
    try:
        return Issue.model_validate(result)
    except ValidationError as exc:
        raise RepairNeeded(validation_details(exc)) from exc


def validate_analysis(content: str, vacancy: dict[str, Any], repair: Callable | None = None) -> AnalysisResult:
    vacancy_id = vacancy.get("id", "unknown")
    data = normalize_vacancy(vacancy)
    logger.debug("Вакансия %s: ответ ИИ для проверки: %s", vacancy_id, content)
    try:
        raw_issues = parse_response(content)
    except ModelResponseError as exc:
        logger.warning("Вакансия %s: повреждён весь ответ модели: %s", vacancy_id, exc)
        return AnalysisResult(issues=[], status="error", error=str(exc))
    issues, reasons, seen = [], [], set()
    for index, raw_issue in enumerate(raw_issues, 1):
        logger.debug("Вакансия %s: замечание #%s до валидации: %s", vacancy_id, index, json.dumps(raw_issue, ensure_ascii=False))
        try:
            try:
                issue = normalize_issue(raw_issue, data, vacancy_id)
            except RepairNeeded as exc:
                if repair is None:
                    raise
                # Ровно один точечный запрос, повторной починки результата нет.
                logger.info("Вакансия %s: точечная починка замечания #%s: %s", vacancy_id, index, exc)
                issue = normalize_issue(repair(raw_issue, str(exc)), data, vacancy_id)
            fingerprint = (issue.topic, issue.source_field, issue.type)
            if fingerprint in seen:
                raise ValueError("Повтор замечания по теме, полю и типу")
            seen.add(fingerprint)
            issues.append(issue)
        except (ValueError, ModelResponseError, ServiceError) as exc:
            reason = f"Замечание #{index}: {validation_details(exc) if isinstance(exc, ValueError) else str(exc)}"
            reasons.append(reason)
            logger.warning("Вакансия %s: замечание #%s отброшено: %s", vacancy_id, index, reason)
    # Противоречия идут перед прочими замечаниями; лимит применяется после объединения с правилами.
    issues.sort(key=lambda i: i.type != "contradiction")
    for issue in issues[MAX_ISSUES:]:
        reasons.append(f"Превышен лимит {MAX_ISSUES}: {issue.topic}/{issue.source_field}")
    issues = issues[:MAX_ISSUES]
    if reasons and not issues and raw_issues:
        logger.warning("Вакансия %s: все замечания ИИ отброшены, status=partial", vacancy_id)
    logger.info("Вакансия %s: принято замечаний=%s, отброшено=%s", vacancy_id, len(issues), len(reasons))
    return AnalysisResult(issues=issues, status="partial" if reasons else "ok", dropped_count=len(reasons), dropped_reasons=reasons)


def merge_results(result: AnalysisResult, rules: list[Issue], vacancy_id: str) -> AnalysisResult:
    combined, seen = [], set()
    reasons = result.dropped_reasons.copy()
    for issue in [*rules, *result.issues]:
        key = (issue.topic, issue.source_field, issue.type)
        if key in seen or len(combined) >= MAX_ISSUES:
            reason = f"Замечание ИИ исключено при объединении: {key} (дубликат или лимит)"
            reasons.append(reason)
            logger.warning("Вакансия %s: %s", vacancy_id, reason)
            continue
        seen.add(key)
        combined.append(issue)
    status = result.status if result.status == "error" else "partial" if reasons else "ok"
    return result.model_copy(update={"issues": combined, "status": status, "dropped_count": len(reasons), "dropped_reasons": reasons})


class VacancyAnalyzer:
    def __init__(self, client: GigaChatClient, prompt: str):
        self.client = client
        self.prompt = prompt

    def analyze(self, vacancy: dict[str, Any]) -> AnalysisResult:
        vacancy_id = vacancy.get("id", "unknown")
        data = normalize_vacancy(vacancy)
        rules = deterministic_issues(data)
        logger.info("Вакансия %s: начало анализа; детерминированных замечаний=%s", vacancy_id, len(rules))
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Вакансия %s: исходные данные: %s", vacancy_id, json.dumps(vacancy, ensure_ascii=False, indent=2))
            logger.debug("Вакансия %s: данные для ИИ: %s", vacancy_id, json.dumps(data, ensure_ascii=False, indent=2))

        def repair(raw, error):
            content = self.client.repair_issue(self.prompt, data, raw, error)
            logger.debug("Вакансия %s: ответ точечной починки: %s", vacancy_id, content)
            repaired = parse_response(content)
            if len(repaired) != 1:
                raise ModelResponseError("Точечная починка должна вернуть ровно одно замечание")
            return repaired[0]

        for attempt in range(4):
            try:
                content = self.client.complete(self.prompt, data)
                parse_response(content)
            except (ModelResponseError, ValidationError) as exc:
                reason = validation_details(exc) if isinstance(exc, ValidationError) else str(exc)
                logger.warning("Вакансия %s: повреждён ответ, попытка %s/4: %s", vacancy_id, attempt + 1, reason)
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                logger.error("Вакансия %s: ответ не получен после 4 попыток, status=error", vacancy_id)
                return AnalysisResult(issues=rules, status="error", error=reason)
            return merge_results(validate_analysis(content, vacancy, repair=repair), rules, vacancy_id)
        raise RuntimeError("Недостижимая ветка анализа")
