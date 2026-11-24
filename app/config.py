from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    gigachat_auth_key: SecretStr = SecretStr("")
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_model: str = "GigaChat-2"
    gigachat_oauth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    gigachat_api_url: str = "https://api.giga.chat/v1"
    gigachat_ca_bundle: str = ""
    source_url: str = "https://opendata.trudvsem.ru/api/v1/vacancies/region/39"
    max_vacancies: int = Field(default=100, ge=1, le=10000)
    page_size: int = Field(default=50, ge=1, le=100)
    request_timeout: float = Field(default=90, gt=0)
    schedule_enabled: bool = False
    schedule_cron: str = "0 9 * * *"
    timezone: str = "Europe/Kaliningrad"
    report_path: Path = ROOT / "reports/latest.json"
    prompt_path: Path = ROOT / "system_prompt.txt"

    @model_validator(mode="after")
    def validate_schedule(self):
        ZoneInfo(self.timezone)
        CronTrigger.from_crontab(self.schedule_cron, timezone=self.timezone)
        if not self.report_path.is_absolute():
            self.report_path = ROOT / self.report_path
        return self
