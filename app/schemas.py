from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VacancyEnvelope(BaseModel):
    vacancy: dict[str, Any]


class VacancyResults(BaseModel):
    vacancies: list[VacancyEnvelope]


class SourceResponse(BaseModel):
    status: str | int = "200"
    meta: dict[str, Any] = Field(default_factory=dict)
    results: VacancyResults


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["ambiguous", "missing", "contradiction"]
    topic: Literal["title", "duties", "requirements", "salary", "schedule", "workplace", "employment"]
    source_field: str = Field(min_length=1)
    quote: str
    second_field: str | None = None
    second_quote: str | None = None
    problem: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence(self):
        if self.type == "missing" and self.quote != "":
            raise ValueError("Для пропуска цитата должна быть пустой")
        if self.type != "missing" and not self.quote:
            raise ValueError("Нужна непустая цитата")
        if self.type == "contradiction":
            if not self.second_field or not self.second_quote:
                raise ValueError("Для противоречия нужны второе поле и цитата")
        elif self.second_field is not None or self.second_quote is not None:
            raise ValueError("Второй источник разрешён только для противоречия")
        return self


MAX_ISSUES = 6


class Analysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issues: list[Issue] = Field(max_length=MAX_ISSUES)


class AnalysisResult(Analysis):
    status: Literal["ok", "partial", "error"] = "ok"
    dropped_count: int = Field(default=0, ge=0)
    dropped_reasons: list[str] = Field(default_factory=list)
    error: str | None = None


class VacancyReport(AnalysisResult):
    id: str
    url: str | None = None


class Report(BaseModel):
    created_at: datetime
    source: str
    provider: str = "GigaChat"
    model: str
    total: int
    vacancies: list[VacancyReport]
