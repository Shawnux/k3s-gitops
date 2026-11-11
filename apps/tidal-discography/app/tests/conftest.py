import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_tidal import FakeTidal  # noqa: E402

from tidal_disco import cli, config  # noqa: E402
from tidal_disco.api import Auth, Client  # noqa: E402


@pytest.fixture
def fake():
    server = FakeTidal().start()
    yield server
    server.stop()


@pytest.fixture
def run(fake, tmp_path, monkeypatch, capsys):
    """run('sync', ...) against the fake; returns (exit code, stdout)."""
    overrides = {}

    def make_client(cfg, state):
        auth = Auth(cfg.client_id, cfg.client_secret, state, cfg.refresh_token, cfg.token_url)
        return Client(auth, cfg.api_base, cfg.country_code, rps=0, sleep=lambda s: None)

    def load(path=None, env=None):
        cfg = config.Config(
            client_id="cid", refresh_token="rt-0", api_base=fake.url + "/v2",
            token_url=fake.url + "/token", state_path=str(tmp_path / "state.db"),
            requests_per_second=0,
        )
        for key, value in overrides.items():
            setattr(cfg, key, value)
        cfg.validate()
        return cfg

    monkeypatch.setattr(cli, "_client", make_client)
    monkeypatch.setattr(cli.config_mod, "load", load)

    def go(*argv, **cfg_overrides):
        overrides.clear()
        overrides.update(cfg_overrides)
        capsys.readouterr()
        code = cli.main(list(argv))
        return code, capsys.readouterr().out

    go.state_path = str(tmp_path / "state.db")
    return go
