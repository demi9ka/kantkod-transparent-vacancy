import json
import re
from typing import Any

from pydantic import ValidationError

from app.errors import ServiceError
from app.schemas import Analysis
from app.services.gigachat import GigaChatClient

FIELDS = {
    "job-name", "duty", "requirements", "requirement.education", "requirement.experience",
    "requirement.qualification", "salary_min", "salary_max", "employment", "schedule",
    "addresses.address[].location",
}


def field_values(vacancy: dict[str, Any], path: str) -> list[str]:
    """Разрешает пути requirement.*, address[0].location и address[].location."""
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


def validate_analysis(content: str, vacancy: dict[str, Any]) -> Analysis:
    # Допускается только одна JSON-конструкция, опционально обёрнутая в markdown.
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    analysis = Analysis.model_validate(json.loads(content))
    seen = set()
    for issue in analysis.issues:
        for path, quote in [(issue.source_field, issue.quote), (issue.second_field, issue.second_quote)]:
            if path is None:
                continue
            canonical = re.sub(r"\[\d+\]", "[]", path)
            if canonical not in FIELDS:
                raise ValueError(f"Неизвестное поле {path!r}. Допустимые поля: {', '.join(sorted(FIELDS))}")
            values = field_values(vacancy, path)
            if quote and not any(quote in value for value in values):
                raise ValueError(f"Цитата {quote!r} отсутствует в поле {path!r}. Исходные значения: {values!r}")
        fingerprint = (issue.type, issue.topic, issue.source_field, issue.quote, issue.second_field, issue.second_quote)
        if fingerprint in seen:
            raise ValueError("Повтор замечания")
        seen.add(fingerprint)
    return analysis


class VacancyAnalyzer:
    def __init__(self, client: GigaChatClient, prompt: str):
        self.client = client
        self.prompt = prompt

    def analyze(self, vacancy: dict[str, Any]) -> Analysis:
        previous_content = None
        validation_error = None
        for attempt in range(2):
            content = self.client.complete(
                self.prompt, vacancy, correction=bool(attempt),
                previous_content=previous_content, validation_error=validation_error,
            )
            try:
                return validate_analysis(content, vacancy)
            except (ValueError, ValidationError) as exc:
                previous_content = content
                validation_error = str(exc)
                if attempt == 1:
                    raise ServiceError(
                        f"Анализ вакансии {vacancy['id']} не прошёл проверку структуры или цитат"
                    ) from None
        raise ServiceError("Не удалось проанализировать вакансию")
