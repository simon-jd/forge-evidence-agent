from pathlib import Path

from forge.cli.client import ClientSettings
from forge.runtime.test_runner import instrument
from forge.settings import Settings


def test_dotenv_provider_secrets_are_loaded_but_never_serialized(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=fixture-openai\nANTHROPIC_API_KEY=fixture-anthropic\n")
    settings = Settings(_env_file=path)
    assert settings.openai_api_key and settings.openai_api_key.get_secret_value() == "fixture-openai"
    assert settings.anthropic_api_key and settings.anthropic_api_key.get_secret_value() == "fixture-anthropic"
    assert "fixture-openai" not in repr(settings) + settings.model_dump_json()
    assert "openai_api_key" not in settings.model_dump()


def test_cli_reads_its_token_and_url_from_dotenv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("FORGE_API_TOKEN", raising=False)
    monkeypatch.delenv("FORGE_API_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("FORGE_API_TOKEN=fixture-client-token\nFORGE_API_URL=http://127.0.0.1:19001\nOPENAI_API_KEY=irrelevant\n")
    settings = ClientSettings()
    assert settings.api_token.get_secret_value() == "fixture-client-token"
    assert settings.api_url == "http://127.0.0.1:19001"
    assert "OPENAI_API_KEY" not in settings.model_dump()


def test_only_supported_test_runners_receive_completion_probes() -> None:
    for argv in (["python", "-I", "-m", "pytest", "-q"], ["python", "-B", "-m", "unittest", "discover"], ["pytest", "tests/test_x.py"]):
        instrumented, framework = instrument(argv, "nonce")
        assert framework in {"pytest", "unittest"} and instrumented[:3] == ["python", "-I", "-c"]
    argv = ["python", "custom_acceptance.py"]
    assert instrument(argv, "nonce") == (argv, None)
