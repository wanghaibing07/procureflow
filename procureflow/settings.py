from dataclasses import dataclass, field
from os import getenv


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    buyer_token: str = field(repr=False)
    approver_token: str = field(repr=False)
    erp_url: str
    erp_token: str = field(repr=False)
    model_url: str = ""
    model_api_key: str = field(default="", repr=False)
    model_name: str = ""
    load_demo: bool = True

    def validate(self) -> None:
        tokens = (self.buyer_token, self.approver_token, self.erp_token)
        if any(len(token) < 16 for token in tokens) or len(set(tokens)) != len(tokens):
            raise ValueError("Configure three distinct tokens of at least 16 characters.")
        if any((self.model_url, self.model_api_key, self.model_name)) and not all(
            (self.model_url, self.model_api_key, self.model_name)
        ):
            raise ValueError("Configure all three model settings or leave them empty.")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=getenv("PROCUREFLOW_DATABASE_URL", "sqlite:///procureflow.db"),
            buyer_token=getenv("PROCUREFLOW_BUYER_TOKEN", ""),
            approver_token=getenv("PROCUREFLOW_APPROVER_TOKEN", ""),
            erp_url=getenv("PROCUREFLOW_ERP_URL", "http://127.0.0.1:9001"),
            erp_token=getenv("PROCUREFLOW_ERP_TOKEN", ""),
            model_url=getenv("PROCUREFLOW_MODEL_URL", ""),
            model_api_key=getenv("PROCUREFLOW_MODEL_API_KEY", ""),
            model_name=getenv("PROCUREFLOW_MODEL_NAME", ""),
            load_demo=getenv("PROCUREFLOW_LOAD_DEMO", "1") == "1",
        )
