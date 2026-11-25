"""Консервативные проверки наличия сведений во всех передаваемых полях."""
import re
from typing import Any, Literal

from app.schemas import Issue

TEXT_FIELDS = ("job-name", "duty", "requirements", "requirement.qualification", "schedule", "employment", "addresses")
Evidence = Literal["present", "absent", "unknown"]


def empty_field(field: str, value: Any) -> bool:
    if value is None or str(value).strip() == "":
        return True
    if field in ("salary_min", "salary_max"):
        try:
            return float(str(value).replace(" ", "").replace(",", ".")) == 0
        except ValueError:
            pass
    return False


def all_text(data: dict) -> str:
    return "\n".join(str(data.get(f) or "") for f in TEXT_FIELDS).lower().replace("ё", "е")


def remote_work(data: dict) -> bool:
    text = all_text(data)
    standalone_remote = any(re.fullmatch(r"(?:полностью\s+)?(?:дистанционн\w*|удаленн\w*|удаленно)", str(data.get(f) or "").strip().lower().replace("ё", "е")) for f in TEXT_FIELDS)
    remote = re.search(r"(?:работ\w*\s*[:—-]?\s+(?:полностью\s+)?(?:дистанцион\w*|удален\w*)|(?:дистанцион\w*|удален\w*)\s+(?:работ\w*|формат\w*)|на\s+дому)", text)
    if not remote and not standalone_remote:
        return False
    if remote and re.search(r"\bне\s*$", text[max(0, remote.start() - 5):remote.start()]):
        return False
    return not re.search(r"обязательн\w*.{0,40}(?:присутств\w*\s+в\s+офисе|работ\w*\s+в\s+офисе)", text)


def address_known(data: dict) -> bool:
    text = all_text(data)
    street = r"(?:\bул\.?\s|\bулиц\w*\s|\bпроспект\w*\s|\bпросп\.?\s|\bпр-т\s|\bшоссе\s|\bпереул\w*\s|\bпер\.?\s|\bнабережн\w*\s|\bнаб\.?\s|\bбульвар\w*\s|\bпроезд\w*\s|\bплощад\w*\s)"
    street_house = re.search(
        street + r"[^\n;]{0,80}[а-я][^\n;]{0,40}?(?:,\s*|\s+)(?:д(?:ом)?\.?\s*)?\d+[а-я]?(?:[/\-]\d+)?(?=\s*(?:$|\n|[.;,)]|корп|строен|к\.))", text,
    )
    landmark = re.search(r"\b(?:ориентир|территори[яи]|здание|корпус|павильон|проходная)\s*[:№]?\s*(?!не\b|нет\b|отсутств|уточн)[\w«\"]+", text)
    named_landmark = re.search(r"\b(?:тц|бц|школ[аеу]|больниц[аеу]|вокзал|завод)\s*(?:№\s*\d+|[«\"][^»\"\n]+[»\"])", text)
    # Адрес с номером дома может быть без улицы: посёлок, дом 12.
    house = re.search(r"\b(?:дом|д\.)\s*\d+[а-я]?", text)
    return bool(street_house or landmark or named_landmark or house)


def employment_known(data: dict) -> bool:
    return not empty_field("employment", data.get("employment")) or bool(re.search(
        r"полн\w*\s+(?:занятост|рабоч\w*\s+день)|неполн\w*\s+(?:день|рабоч\w*\s+(?:день|недел))|частичн\w*\s+занятост|\bсменн|\bвахт(?:а\b|ов)|(?<![а-яa-z])ставк|совместительств|подработк",
        all_text(data),
    ))


def requirements_known(data: dict) -> bool:
    if any(not empty_field(f, data.get(f)) for f in ("requirements", "requirement.education", "requirement.qualification")):
        return True
    return bool(re.search(r"требован|требуется\s+опыт|образован|квалификац|навык|умени[ея]|знани[ея]|опыт\s+(?:не\s+)?(?:требуется|работы)", all_text(data)))


def salary_in_text(data: dict) -> bool:
    return bool(re.search(
        r"(?:зарплат\w*|заработн\w*\s+плат\w*|оклад\w*|оплат\w*|доход\w*|з/п)[^\n\d]{0,45}\d[\d\s.,–—-]*(?:руб|₽|тыс)",
        all_text(data),
    ))


def missing_evidence(issue: dict, data: dict) -> Evidence:
    """Оценивает именно искомое сведение; неизвестность не выдаётся за пропуск."""
    field = issue.get("source_field")
    topic = issue.get("topic")
    description = str(issue.get("problem", "")).lower().replace("ё", "е")
    if field in ("salary_min", "salary_max"):
        if not empty_field(field, data.get(field)) or salary_in_text(data):
            return "present"
        return "absent"
    if field == "requirement.experience":
        if not empty_field(field, data.get(field)):
            return "present"
        return "present" if re.search(r"опыт", all_text(data)) else "absent"
    if topic == "employment":
        return "present" if employment_known(data) else "absent"
    if topic == "workplace" and (field == "addresses" or re.search(r"адрес|мест[оа]\s+работ|где.*работ", description)):
        return "present" if remote_work(data) or address_known(data) else "absent"
    if topic == "requirements":
        # Дополнительные сертификаты и специализации не обязательны сами по себе.
        if re.search(r"сертификат|дополнительн\w*\s+образован|специализац", description):
            return "unknown"
        return "present" if requirements_known(data) else "absent"
    if topic == "schedule":
        text = all_text(data)
        wanted = []
        if re.search(r"час|врем[яе]|начал|окончан|длительност", description):
            wanted.append(bool(re.search(r"\d{1,2}[:.]\d{2}|\d+\s*час|с\s*\d{1,2}\s*(?:до|[-–—])\s*\d{1,2}", text)))
        if re.search(r"дн[иея]|дней|недел", description):
            wanted.append(bool(re.search(r"\b[1-7]\s*/\s*[1-7]\b|пятиднев|понедельник|пятниц|2\s*через\s*2", text)))
        if wanted:
            return "present" if all(wanted) else "unknown" if any(wanted) else "absent"
        if not empty_field("schedule", data.get("schedule")) or re.search(r"график|режим\s+работ", text):
            return "present" if re.search(r"\d{1,2}[:.]\d{2}|\b[1-7]/[1-7]\b|понедельник|пятиднев", text) else "unknown"
        return "absent"
    if topic == "title":
        if not empty_field("job-name", data.get("job-name")):
            return "present"
        if re.search(r"(?:должность|вакансия|вакансию)\s*[:«]\s*\w+", all_text(data)):
            return "present"
        return "unknown" if any(data.get(f) for f in ("duty", "requirements", "requirement.qualification")) else "absent"
    if topic == "duties":
        if not empty_field("duty", data.get("duty")) or re.search(r"обязанност|задач[иа]|в\s+обязанности", all_text(data)):
            return "present"
        return "absent"
    return "unknown"


def deterministic_issues(data: dict) -> list[Issue]:
    result = []

    def add(topic, field, problem, recommendation):
        result.append(Issue(type="missing", topic=topic, source_field=field, quote="", problem=problem, recommendation=recommendation))

    if not employment_known(data):
        add("employment", "employment", "Не указан тип занятости.", "Уточнить тип занятости.")
    low = empty_field("salary_min", data.get("salary_min"))
    high = empty_field("salary_max", data.get("salary_max"))
    if not salary_in_text(data):
        if low and high:
            add("salary", "salary_min", "Не указан размер оплаты труда.", "Указать размер оплаты труда или диапазон зарплаты.")
        elif low or high:
            field = "salary_min" if low else "salary_max"
            add("salary", field, "Указана только одна граница зарплаты: вилка неполная.", "Уточнить отсутствующую границу зарплаты или указать, что сумма фиксированная.")
    if not requirements_known(data):
        add("requirements", "requirements", "Не описаны требования к кандидату.", "Уточнить необходимые знания, навыки или образование либо указать, что специальных требований нет.")
    if not remote_work(data) and not address_known(data):
        add("workplace", "addresses", "Неясно, где выходить на работу: нет адреса или ориентира.", "Указать адрес рабочего места или понятный ориентир.")
    return result
