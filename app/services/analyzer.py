import json
import logging
import re
from typing import Any

from pydantic import ValidationError

from app.errors import ModelResponseError
from app.schemas import MAX_ISSUES, Analysis, Issue
from app.services.gigachat import GigaChatClient

FIELDS = {
    "job-name", "duty", "requirements", "requirement.education", "requirement.experience",
    "requirement.qualification", "salary_min", "salary_max", "employment", "schedule",
    "addresses",
}
logger = logging.getLogger(__name__)


def validation_details(error: ValueError) -> str:
    if isinstance(error, ValidationError):
        return str(error.errors(include_input=False, include_url=False))
    return str(error)


def field_values(vacancy: dict[str, Any], path: str) -> list[str]:
    """Разрешает пути requirement.*, address[0].location и address[].location."""
    if path in vacancy and isinstance(vacancy[path], (str, int, float)) and not isinstance(vacancy[path], bool):
        return [str(vacancy[path])]
    nodes: list[Any] = [vacancy]
    for part in path.split("."):
        match = re.fullmatch(r"([^\[\]]+)(?:\[(\d*)\])?", part)
        if not match:
            return []
        key, index = match.groups()
        next_nodes = []
        for node in nodes:
            if not isinstance(node, dict) or key not in node:
                continue
            value = node[key]
            if index is None:
                next_nodes.append(value)
            elif isinstance(value, list):
                if index == "":
                    next_nodes.extend(value)
                elif int(index) < len(value):
                    next_nodes.append(value[int(index)])
        nodes = next_nodes
    return [str(node) for node in nodes if isinstance(node, (str, int, float)) and not isinstance(node, bool)]


def normalize_vacancy(vacancy: dict[str, Any]) -> dict[str, str | None]:
    """Формат входа модели: только поля из промпта, значения — текст или null."""
    result = {}
    for field in sorted(FIELDS):
        path = "addresses.address[].location" if field == "addresses" and isinstance(vacancy.get(field), dict) else field
        values = field_values(vacancy, path)
        result[field] = "\n".join(values) if values else None
    return result


def validate_analysis(content: str, vacancy: dict[str, Any]) -> Analysis:
    # Допускается только одна JSON-конструкция, опционально обёрнутая в markdown.
    vacancy_id = vacancy.get("id", "unknown")
    normalized = normalize_vacancy(vacancy)
    try:
        if not isinstance(content, str):
            raise ValueError("Ответ модели должен быть текстом JSON")
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        data = json.loads(content)
        if not isinstance(data, dict) or set(data) != {"issues"} or not isinstance(data["issues"], list):
            raise ValueError('Ожидается объект {"issues": [...]} без дополнительных полей')
    except ValueError as exc:
        logger.warning("Вакансия %s: ответ модели отброшен: %s", vacancy_id, validation_details(exc))
        return Analysis(issues=[])

    issues = []
    seen = set()
    for index, raw_issue in enumerate(data["issues"], start=1):
        try:
            issue = Issue.model_validate(raw_issue)
            for path, quote in [(issue.source_field, issue.quote), (issue.second_field, issue.second_quote)]:
                if path is None:
                    continue
                if path not in FIELDS:
                    raise ValueError(f"Неизвестное поле {path!r}")
                values = field_values(normalized, path)
                if quote and not any(quote in value for value in values):
                    raise ValueError(f"Цитата {quote!r} отсутствует в поле {path!r}")
            fingerprint = (issue.type, issue.topic, issue.source_field, issue.quote, issue.second_field, issue.second_quote)
            if fingerprint in seen:
                raise ValueError("Повтор замечания")
            if len(issues) >= MAX_ISSUES:
                raise ValueError(f"Превышен лимит {MAX_ISSUES} замечаний на вакансию")
        except ValueError as exc:
            logger.warning(
                "Вакансия %s: замечание #%s отброшено: %s", vacancy_id, index, validation_details(exc)
            )
            continue
        seen.add(fingerprint)
        issues.append(issue)
    return Analysis(issues=issues)


class VacancyAnalyzer:
    def __init__(self, client: GigaChatClient, prompt: str):
        self.client = client
        self.prompt = prompt

    def analyze(self, vacancy: dict[str, Any]) -> Analysis:
        try:
            content = self.client.complete(self.prompt, normalize_vacancy(vacancy))
        except (ModelResponseError, ValidationError) as exc:
            details = validation_details(exc) if isinstance(exc, ValidationError) else str(exc)
            logger.warning("Вакансия %s: ответ модели отброшен: %s", vacancy.get("id", "unknown"), details)
            return Analysis(issues=[])
        return validate_analysis(content, vacancy)
