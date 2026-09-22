"""Runtime settings from environment / Function App settings."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from models import Scope


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    mg_id: str
    subscription_ids: str = ""
    resource_types: str = ""
    law_resource_id: str = ""
    logs_ingestion_endpoint: str = ""
    findings_dcr_immutable_id: str = ""
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

    def resource_type_list(self, configured: list[str]) -> list[str]:
        """RESOURCE_TYPES narrows the configured types (staged rollout); unknown names fail."""
        wanted = [s.strip() for s in self.resource_types.split(",") if s.strip()]
        if not wanted:
            return configured
        unknown = [w for w in wanted if w not in configured]
        if unknown:
            raise ValueError(f"RESOURCE_TYPES names unconfigured types: {unknown}")
        return [c for c in configured if c in wanted]

    @property
    def scope(self) -> Scope:
        subs = self.subscription_id_list
        if subs:
            return Scope(kind="subscriptions", values=subs)
        return Scope(kind="management_group", values=[self.mg_id])
