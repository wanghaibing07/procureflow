from secrets import token_hex

from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from procureflow.api import create_app
from procureflow.db import Base
from procureflow.settings import Settings


def test_readiness_tracks_database_and_recovers_without_exposing_error(tmp_path):
    settings = Settings(
        f"sqlite:///{tmp_path / 'inventory.db'}", token_hex(24), token_hex(24),
        "http://127.0.0.1:1", token_hex(24),
    )
    with TestClient(create_app(settings, erp=object())) as client:
        liveness = client.get("/healthz")
        assert liveness.status_code == 200
        assert liveness.json() == {"alive": True, "model_configured": False}
        healthy = client.get("/readyz")
        assert healthy.status_code == 200
        assert healthy.json()["ready"] is True

        engine = client.app.state.db.engine

        def fail_checkout(*_args):
            raise OperationalError("private-host", {}, Exception("private-secret"))

        event.listen(engine, "checkout", fail_checkout)
        try:
            failed = client.get("/readyz")
            assert failed.status_code == 503
            assert failed.json()["ready"] is False
            assert "private-host" not in failed.text and "private-secret" not in failed.text
            # A failed dependency is not a dead application process.
            liveness = client.get("/healthz")
            assert liveness.status_code == 200 and liveness.json()["alive"] is True
        finally:
            event.remove(engine, "checkout", fail_checkout)

        assert client.get("/readyz").status_code == 200

        with engine.begin() as connection:
            connection.execute(text("DROP TABLE catalog_quotes"))
        assert client.get("/readyz").status_code == 503
        Base.metadata.tables["catalog_quotes"].create(engine)
        assert client.get("/readyz").status_code == 200
