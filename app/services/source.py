from typing import Any
import logging

import requests
from pydantic import ValidationError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.config import Settings
from app.errors import ServiceError
from app.schemas import SourceResponse

logger = logging.getLogger(__name__)


def extract_vacancies(data: SourceResponse) -> list[dict[str, Any]]:
    if str(data.status) != "200":
        raise ServiceError("Источник сообщил об ошибке в поле status")
    result = []
    for item in data.results.vacancies:
        vacancy = item.vacancy
        identifier = vacancy.get("id")
        if not isinstance(identifier, (str, int)) or isinstance(identifier, bool) or not str(identifier).strip():
            raise ServiceError("У вакансии отсутствует корректный id", status_code=422)
        result.append(vacancy)
    return result


class VacancySource:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    def close(self):
        self.session.close()

    def fetch(self) -> list[dict[str, Any]]:
        vacancies: dict[str, dict[str, Any]] = {}
        offset = 0
        # Даже при небольшом лимите запрашиваем две страницы, если есть данные.
        page_size = min(self.settings.page_size, max(1, (self.settings.max_vacancies + 1) // 2))
        while len(vacancies) < self.settings.max_vacancies:
            try:
                response = self.session.get(
                    self.settings.source_url,
                    params={"limit": page_size, "offset": offset},
                    timeout=self.settings.request_timeout,
                )
                response.raise_for_status()
                data = SourceResponse.model_validate(response.json())
            except (requests.RequestException, ValueError, ValidationError) as exc:
                raise ServiceError("Не удалось загрузить вакансии с портала «Работа в России»") from exc
            page = extract_vacancies(data)
            logger.info("Страница %s: получено %s; offset=%s, limit=%s", offset + 1, len(page), offset, page_size)
            if not page:
                break
            new_ids = {str(item["id"]) for item in page} - vacancies.keys()
            if not new_ids:
                raise ServiceError("Источник повторяет страницу: загрузка остановлена")
            for vacancy in page:
                vacancies[str(vacancy["id"])] = vacancy
                if len(vacancies) >= self.settings.max_vacancies:
                    break
            if len(page) < page_size:
                break
            # У API «Работа в России» offset — номер страницы, начиная с 0.
            offset += 1
        return list(vacancies.values())
