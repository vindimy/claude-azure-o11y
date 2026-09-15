"""Runtime settings from environment / Function App settings."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from models import Scope


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    mg_id: str
    subscription_ids: str = ""
    law_resource_id: str = ""
    storage_account_name: str = ""
    reports_container: str = "reports"
    suppression_container: str = "suppression"
    ops_teams_webhook_url: str = ""
    dry_run: bool = False
    output_dir: Path = Path("./out")
    config_dir: Path = Path("config")
    identity_file: Path = Path("identity/role-requirements.yaml")
    max_concurrency: int = 8
    pricing_currency: str = "USD"
    batch_size: int = 50

    @property
    def subscription_id_list(self) -> list[str]:
        return [s.strip() for s in self.subscription_ids.split(",") if s.strip()]

    @property
    def scope(self) -> Scope:
        subs = self.subscription_id_list
        if subs:
            return Scope(kind="subscriptions", values=subs)
        return Scope(kind="management_group", values=[self.mg_id])
