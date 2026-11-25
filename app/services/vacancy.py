import re
from typing import Any

FIELDS = {
    "job-name", "duty", "requirements", "requirement.education", "requirement.experience",
    "requirement.qualification", "salary_min", "salary_max", "employment", "schedule",
    "addresses",
}


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

