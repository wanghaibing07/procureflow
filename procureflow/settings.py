from dataclasses import dataclass, field
from os import getenv

DEFAULT_MODEL_TIMEOUT_SECONDS = 20
MAX_MODEL_TIMEOUT_SECONDS = 180


def validate_model_timeout(seconds: int) -> None:
    if type(seconds) is not int or not 1 <= seconds <= MAX_MODEL_TIMEOUT_SECONDS:
        raise ValueError("Model timeout must be an integer between 1 and 180 seconds.")


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
    model_timeout_seconds: int = DEFAULT_MODEL_TIMEOUT_SECONDS

    def validate(self) -> None:
        tokens = (self.buyer_token, self.approver_token, self.erp_token)
        if any(len(token) < 16 for token in tokens) or len(set(tokens)) != len(tokens):
            raise ValueError("Configure three distinct tokens of at least 16 characters.")
        if any((self.model_url, self.model_api_key, self.model_name)) and not all(
            (self.model_url, self.model_api_key, self.model_name)
        ):
            raise ValueError("Configure all three model settings or leave them empty.")
        if self.model_url:
            validate_model_timeout(self.model_timeout_seconds)

    @classmethod
    def from_env(cls) -> "Settings":
        model_url = getenv("PROCUREFLOW_MODEL_URL", "")
        model_api_key = getenv("PROCUREFLOW_MODEL_API_KEY", "")
        model_name = getenv("PROCUREFLOW_MODEL_NAME", "")
        timeout = DEFAULT_MODEL_TIMEOUT_SECONDS
        if any((model_url, model_api_key, model_name)):
            try:
                timeout = int(getenv("PROCUREFLOW_MODEL_TIMEOUT_SECONDS", str(DEFAULT_MODEL_TIMEOUT_SECONDS)))
            except ValueError:
                raise ValueError("Model timeout must be an integer between 1 and 180 seconds.") from None
            validate_model_timeout(timeout)
        return cls(
            database_url=getenv("PROCUREFLOW_DATABASE_URL", "sqlite:///procureflow.db"),
            buyer_token=getenv("PROCUREFLOW_BUYER_TOKEN", ""),
            approver_token=getenv("PROCUREFLOW_APPROVER_TOKEN", ""),
            erp_url=getenv("PROCUREFLOW_ERP_URL", "http://127.0.0.1:9001"),
            erp_token=getenv("PROCUREFLOW_ERP_TOKEN", ""),
            model_url=model_url,
            model_api_key=model_api_key,
            model_name=model_name,
            load_demo=getenv("PROCUREFLOW_LOAD_DEMO", "1") == "1",
            model_timeout_seconds=timeout,
        )
