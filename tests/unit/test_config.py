"""Synthetic files and process environments; never read developer credentials."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from ezbookkeeping_importer import bootstrap
from ezbookkeeping_importer.config import (
    BusinessMailSettings,
    BusinessSettings,
    ConfigurationError,
    ENV_FIELDS,
    MailSettings,
    Settings,
    load_settings,
)
from ezbookkeeping_importer.entrypoints import cli

BUSINESS = 'timezone = "Asia/Shanghai"\nclassification_mode = "rules_only"\n[mail]\nsource_id = "synthetic"\n'
ENV = {
    "EBKI_DATABASE_URL": "postgresql://synthetic:test-password@example.test/importer",
    "EBKI_LEDGER_URL": "http://ledger.example.test",
    "EBKI_LEDGER_TOKEN": "ledger-test-secret",
    "EBKI_IMAP_USERNAME": "synthetic@example.test",
    "EBKI_IMAP_PASSWORD": "imap-test-secret",
    "EBKI_AI_URL": "https://ai.example.test/v1",
    "EBKI_AI_MODEL": "synthetic-model",
    "EBKI_AI_TOKEN": "ai-test-secret",
}


@pytest.fixture
def config(tmp_path, monkeypatch):
    for variable in ENV_FIELDS.values():
        monkeypatch.delenv(variable, raising=False)
    path = tmp_path / "synthetic.toml"
    path.write_text(BUSINESS)
    return path


def environment(monkeypatch, values=ENV):
    for variable, value in values.items():
        monkeypatch.setenv(variable, value)


@pytest.fixture
def cli_configuration(config, monkeypatch):
    monkeypatch.chdir(config.parent)
    environment(monkeypatch, {"EBKI_DATABASE_URL": ENV["EBKI_DATABASE_URL"]})
    loaded = []

    def runtime(settings, **kwargs):
        loaded.append(settings)
        return SimpleNamespace(store=SimpleNamespace(), schema_version=2, close=lambda: None)

    monkeypatch.setattr(cli, "Runtime", runtime)
    return loaded


def test_cli_reads_default_data_configuration(cli_configuration, monkeypatch):
    Path("data").mkdir()
    Path("data/config.toml").write_text(BUSINESS.replace("synthetic", "data-source"))
    monkeypatch.setattr("sys.argv", ["ebki", "migrate"])

    assert cli.main() == 0
    assert len(cli_configuration) == 1
    assert cli_configuration[0].mail.source_id == "data-source"


@pytest.mark.parametrize("default_exists", [False, True])
def test_cli_explicit_configuration_overrides_default(
    cli_configuration, monkeypatch, default_exists
):
    if default_exists:
        Path("data").mkdir()
        Path("data/config.toml").write_text("invalid = [")
    Path("chosen.toml").write_text(BUSINESS.replace("synthetic", "explicit-source"))
    monkeypatch.setattr("sys.argv", ["ebki", "--config", "chosen.toml", "migrate"])

    assert cli.main() == 0
    assert len(cli_configuration) == 1
    assert cli_configuration[0].mail.source_id == "explicit-source"


def test_cli_missing_default_configuration_initializes_defaults(
    cli_configuration, monkeypatch, capsys
):
    import json

    monkeypatch.setattr("sys.argv", ["ebki", "migrate"])

    assert cli.main() == 0
    assert len(cli_configuration) == 1
    assert cli_configuration[0].mail.source_id == "qq-primary"
    assert cli_configuration[0].classification_mode == "ai"
    assert json.loads(capsys.readouterr().out) == {"schema_version": 2}
    assert Path("data/config.toml").is_file()


def test_cli_missing_explicit_configuration_reports_error(
    cli_configuration, monkeypatch, capsys
):
    import json

    Path("data").mkdir()
    Path("data/config.toml").write_text(BUSINESS)
    monkeypatch.setattr("sys.argv", ["ebki", "--config", "missing.toml", "migrate"])
    assert cli.main() == 1
    assert cli_configuration == []
    assert json.loads(capsys.readouterr().err)["error_type"] == "ConfigurationError"


@pytest.mark.parametrize("log_level", [None, "WARNING"])
def test_runtime_paths_rotation_and_business_log_level(
    config, monkeypatch, log_level
):
    environment(monkeypatch, {"EBKI_DATABASE_URL": ENV["EBKI_DATABASE_URL"]})
    if log_level is not None:
        config.write_text(f'log_level = "{log_level}"\n' + BUSINESS)
    settings = load_settings(str(config), command="status")
    assert (settings.evidence_dir, settings.report_dir, settings.log_dir) == (
        Path("data/email"),
        Path("data/reports"),
        Path("data/logs"),
    )
    assert settings.log_level == (log_level or "INFO")
    assert settings.log_max_bytes == 10_485_760
    assert settings.log_backups == 5


@pytest.mark.parametrize(
    "field",
    sorted(Settings.model_fields.keys() - BusinessSettings.model_fields.keys()),
)
def test_runtime_only_fields_are_not_business_toml(config, monkeypatch, field):
    environment(monkeypatch)
    config.write_text(f'{field} = "SECRET_VALUE"\n' + BUSINESS)
    with pytest.raises(ConfigurationError) as error:
        load_settings(str(config))
    assert f"{field}: unknown TOML field" in str(error.value)
    assert "SECRET_VALUE" not in str(error.value)
    assert all(value not in str(error.value) for value in ENV.values())


@pytest.mark.parametrize(
    "field",
    sorted(MailSettings.model_fields.keys() - BusinessMailSettings.model_fields.keys()),
)
def test_mail_connection_fields_are_not_business_toml(config, monkeypatch, field):
    environment(monkeypatch)
    config.write_text(BUSINESS + f'{field} = "SECRET_VALUE"\n')
    with pytest.raises(ConfigurationError) as error:
        load_settings(str(config))
    assert f"mail.{field}: unknown TOML field" in str(error.value)
    assert "SECRET_VALUE" not in str(error.value)


def test_current_business_fields_survive_environment_assembly(config, monkeypatch):
    from datetime import date, time

    environment(monkeypatch)
    config.write_text(
        'date_only_time = 12:34:56\nrepayment_ownership_confirmed = true\n'
        'log_level = "debug"\n' + BUSINESS + 'rescan_days = 14\n'
        '[[repayments]]\nsource_account_id = "source"\ndestination_account_id = "destination"\n'
        'category_id = "repayment"\ncurrency = "CNY"\nvalid_from = 2026-01-01\n'
        'valid_until = 2026-12-31\n[[rules]]\nmerchant_pattern = "synthetic"\n'
        'category_id = "expense"\n'
    )
    settings = load_settings(str(config))
    assert settings.date_only_time == time(12, 34, 56)
    assert settings.repayment_ownership_confirmed is True
    assert settings.log_level == "DEBUG"
    assert settings.mail.source_id == "synthetic" and settings.mail.rescan_days == 14
    assert settings.repayments[0].valid_from == date(2026, 1, 1)
    assert settings.repayments[0].valid_until == date(2026, 12, 31)
    assert settings.repayments[0].destination_account_id == "destination"
    assert settings.rules[0].category_id == "expense"


def test_internal_settings_allow_dependency_injection():
    settings = Settings(
        timezone="Asia/Shanghai", mail=MailSettings(source_id="synthetic"),
        evidence_dir=Path("test/email"), report_dir=Path("test/reports"),
        log_dir=Path("test/logs"), log_max_bytes=512, log_backups=2,
    )
    assert settings.evidence_dir == Path("test/email")
    assert settings.report_dir == Path("test/reports")
    assert settings.log_dir == Path("test/logs")
    assert settings.log_max_bytes == 512 and settings.log_backups == 2


@pytest.mark.parametrize("command", ["migrate", "status", "issues", "sync", "recheck"])
def test_local_commands_only_require_database_connection(config, monkeypatch, command):
    environment(monkeypatch, {"EBKI_DATABASE_URL": ENV["EBKI_DATABASE_URL"]})
    settings = load_settings(str(config), command=command)
    assert settings.database_url.get_secret_value() == ENV["EBKI_DATABASE_URL"]
    assert settings.ledger_url == "" and settings.mail.username == ""


@pytest.mark.parametrize("action", ["accept-source", "ignore", "retry", "confirm-new"])
def test_local_resolutions_need_no_remote_service(config, monkeypatch, action):
    environment(monkeypatch, {"EBKI_DATABASE_URL": ENV["EBKI_DATABASE_URL"]})
    load_settings(str(config), command="resolve", action=action)


@pytest.mark.parametrize(
    "command,action,account_id",
    [("restore-audit", None, None), ("resolve", "link", None), ("resolve", "retry", "123")],
)
def test_ledger_commands_report_all_missing_ledger_variables(
    config, monkeypatch, command, action, account_id
):
    environment(monkeypatch, {"EBKI_DATABASE_URL": ENV["EBKI_DATABASE_URL"]})
    with pytest.raises(ConfigurationError) as error:
        load_settings(str(config), command=command, action=action, account_id=account_id)
    assert "EBKI_LEDGER_URL" in str(error.value) and "EBKI_LEDGER_TOKEN" in str(error.value)
    assert "EBKI_IMAP_PASSWORD" not in str(error.value)


def test_rules_only_needs_no_ai_but_worker_requires_mail_and_ledger(config, monkeypatch):
    environment(monkeypatch, {k: v for k, v in ENV.items() if not k.startswith("EBKI_AI_")})
    settings = load_settings(str(config), command="run")
    assert settings.ai_url is None
    monkeypatch.delenv("EBKI_IMAP_PASSWORD")
    with pytest.raises(ConfigurationError, match="EBKI_IMAP_PASSWORD"):
        load_settings(str(config), command="run")


@pytest.mark.parametrize("command", ["run", "doctor"])
def test_ai_mode_requires_url_model_and_key(config, monkeypatch, command):
    config.write_text(BUSINESS.replace('"rules_only"', '"ai"'))
    environment(monkeypatch, {k: v for k, v in ENV.items() if not k.startswith("EBKI_AI_")})
    with pytest.raises(ConfigurationError) as error:
        load_settings(str(config), command=command)
    assert all(
        name in str(error.value) for name in ("EBKI_AI_URL", "EBKI_AI_MODEL", "EBKI_AI_TOKEN")
    )
    environment(monkeypatch)
    monkeypatch.setenv("EBKI_AI_TOKEN", "   ")
    with pytest.raises(ConfigurationError, match="EBKI_AI_TOKEN"):
        load_settings(str(config), command=command)


def test_every_service_field_comes_from_environment(config, monkeypatch):
    environment(monkeypatch)
    overrides = {
        "EBKI_IMAP_HOST": "imap.example.test",
        "EBKI_IMAP_PORT": "1993",
    }
    environment(monkeypatch, overrides)
    settings = load_settings(str(config))
    for field, variable in ENV_FIELDS.items():
        expected = {**ENV, **overrides}[variable]
        owner, _, name = field.rpartition(".")
        value = getattr(settings.mail if owner else settings, name)
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        assert str(value) == expected
    assert settings.mail.source_id == "synthetic"


def test_no_implicit_dotenv_loader(config, monkeypatch):
    monkeypatch.chdir(config.parent)
    (config.parent / ".env").write_text("EBKI_DATABASE_URL=synthetic-dotenv-secret\n")
    with pytest.raises(ConfigurationError, match="EBKI_DATABASE_URL"):
        load_settings(str(config), command="status")


@pytest.mark.parametrize(
    "variable,value",
    [
        ("EBKI_IMAP_PORT", "0"),
        ("EBKI_IMAP_PORT", "65536"),
        ("EBKI_IMAP_PORT", "secret-not-port"),
        ("EBKI_LEDGER_URL", "https://user:secret@example.test"),
        ("EBKI_AI_URL", "file:///secret"),
        ("EBKI_LEDGER_URL", "https://example.test:99999/secret"),
    ],
)
def test_invalid_values_report_safe_environment_names(config, monkeypatch, variable, value):
    environment(monkeypatch)
    monkeypatch.setenv(variable, value)
    with pytest.raises(ConfigurationError) as error:
        load_settings(str(config))
    assert variable in str(error.value)
    assert "secret" not in str(error.value)
    assert ENV["EBKI_DATABASE_URL"] not in str(error.value)


def test_unknown_toml_key_is_an_error_and_never_displays_value(config, monkeypatch):
    environment(monkeypatch)
    config.write_text('misspelled="secret-typo-value"\n' + BUSINESS)
    with pytest.raises(ConfigurationError) as error:
        load_settings(str(config), command="status")
    assert "misspelled" in str(error.value)
    assert "secret-typo-value" not in str(error.value)


@pytest.mark.parametrize(
    "suffix,field",
    [
        ('misspelled = "SECRET_VALUE"\n', "mail.misspelled"),
        ('[[rules]]\nmerchant_pattern="test"\ncategory_id="category"\n'
         'misspelled="SECRET_VALUE"\n', "rules.0.misspelled"),
        ('[[repayments]]\nsource_account_id="source"\ndestination_account_id="destination"\n'
         'category_id="category"\ncurrency="CNY"\nvalid_from=2026-01-01\n'
         'misspelled="SECRET_VALUE"\n', "repayments.0.misspelled"),
    ],
)
def test_nested_business_tables_reject_unknown_fields(config, monkeypatch, suffix, field):
    environment(monkeypatch)
    config.write_text(BUSINESS + suffix)
    with pytest.raises(ConfigurationError) as error:
        load_settings(str(config))
    assert f"{field}: unknown TOML field" in str(error.value)
    assert "SECRET_VALUE" not in str(error.value)


@pytest.mark.parametrize("value", ['"SECRET_VALUE"', "123", "[]"])
def test_mail_business_input_must_be_a_table(config, monkeypatch, value):
    environment(monkeypatch)
    config.write_text(f'timezone="Asia/Shanghai"\nmail={value}\n')
    with pytest.raises(ConfigurationError) as error:
        load_settings(str(config))
    assert "mail: must be a TOML table" in str(error.value)
    assert "SECRET_VALUE" not in str(error.value)


@pytest.mark.parametrize("command", ["run", "doctor"])
def test_cli_validation_precedes_runtime_and_does_not_leak(config, monkeypatch, capsys, command):
    from ezbookkeeping_importer.entrypoints import run as startup

    monkeypatch.setattr(
        startup, "Runtime", lambda *a, **k: pytest.fail("resource opened before validation")
    )
    environment(monkeypatch)
    monkeypatch.setenv("EBKI_LEDGER_URL", "https://username:CLI-SECRET@example.test")
    monkeypatch.setattr("sys.argv", ["ebki", "--config", str(config), command])
    monkeypatch.setattr(
        cli, "Runtime", lambda *a, **k: pytest.fail("resource opened before validation")
    )
    assert cli.main() == 1
    text = capsys.readouterr().err
    assert "EBKI_LEDGER_URL" in text and "CLI-SECRET" not in text and "input_value" not in text


class Resource:
    def migrate(self, **kwargs):
        return 2

    def check_schema(self):
        return 2

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def constructors(monkeypatch):
    created = {}
    arguments = {}

    def factory(name):
        def construct(*args, **kwargs):
            arguments[name] = (args, kwargs)
            result = Resource()
            created[name] = result
            return result

        return construct

    for name in (
        "PostgresStore",
        "EzBookkeepingClient",
        "AIClient",
        "BankParser",
        "EvidenceStore",
        "MailClient",
    ):
        monkeypatch.setattr(bootstrap, name, factory(name))
    return SimpleNamespace(created=created, arguments=arguments)


@pytest.mark.parametrize(
    "command,action,account_id,expected",
    [
        ("migrate", None, None, {"PostgresStore"}),
        ("status", None, None, {"PostgresStore"}),
        ("issues", None, None, {"PostgresStore"}),
        ("sync", None, None, {"PostgresStore"}),
        ("recheck", None, None, {"PostgresStore"}),
        ("resolve", "ignore", None, {"PostgresStore"}),
        ("resolve", "link", None, {"PostgresStore", "EzBookkeepingClient"}),
        ("resolve", "retry", "123", {"PostgresStore", "EzBookkeepingClient"}),
        ("restore-audit", None, None, {"PostgresStore", "EzBookkeepingClient"}),
        ("doctor", None, None, {"PostgresStore", "EzBookkeepingClient"}),
    ],
)
def test_runtime_constructs_only_command_dependencies(
    config, monkeypatch, constructors, command, action, account_id, expected
):
    environment(monkeypatch)
    settings = load_settings(str(config), command=command, action=action, account_id=account_id)
    runtime = bootstrap.Runtime(settings, command=command, action=action, account_id=account_id)
    assert set(constructors.created) == expected
    runtime.close()
    assert constructors.created["PostgresStore"].closed
    if "EzBookkeepingClient" in expected:
        assert constructors.created["EzBookkeepingClient"].closed


def test_service_timeouts_are_fixed_and_rules_only_does_not_construct_ai(
    config, monkeypatch, constructors
):
    environment(monkeypatch)
    config.write_text(BUSINESS.replace('"rules_only"', '"ai"'))
    settings = load_settings(str(config))
    runtime = bootstrap.Runtime(settings)
    assert constructors.arguments["EzBookkeepingClient"][1]["timeout"] == 30
    assert constructors.arguments["AIClient"][1]["timeout"] == 30
    runtime.mail()
    assert constructors.arguments["MailClient"][0][0] == settings.mail
    runtime.close()
    assert all(
        constructors.created[name].closed
        for name in ("PostgresStore", "EzBookkeepingClient", "AIClient")
    )
    constructors.created.clear()
    config.write_text(BUSINESS)
    runtime = bootstrap.Runtime(load_settings(str(config)))
    assert "AIClient" not in constructors.created
    runtime.close()


def test_constructor_failure_closes_previously_created_resources(config, monkeypatch, constructors):
    environment(monkeypatch)
    settings = load_settings(str(config))

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic constructor failure")

    monkeypatch.setattr(bootstrap, "EvidenceStore", fail)
    with pytest.raises(RuntimeError, match="constructor failure"):
        bootstrap.Runtime(settings)
    assert constructors.created["PostgresStore"].closed
    assert constructors.created["EzBookkeepingClient"].closed


def test_bad_configuration_cannot_open_database_even_with_direct_runtime(
    config, monkeypatch, constructors
):
    environment(monkeypatch, {"EBKI_DATABASE_URL": ENV["EBKI_DATABASE_URL"]})
    settings = load_settings(str(config), command="status")
    with pytest.raises(ConfigurationError, match="EBKI_LEDGER_URL"):
        bootstrap.Runtime(settings)
    assert constructors.created == {}


def test_env_missing_database_is_safe_even_when_other_secrets_are_present(config, monkeypatch):
    environment(
        monkeypatch, {name: value for name, value in ENV.items() if name != "EBKI_DATABASE_URL"}
    )
    with pytest.raises(ConfigurationError) as error:
        load_settings(str(config), command="status")
    assert str(error.value) == "missing required environment variables: EBKI_DATABASE_URL"
    assert all(value not in str(error.value) for value in ENV.values())


def test_toml_syntax_error_does_not_echo_secret(config, monkeypatch):
    environment(monkeypatch)
    config.write_text('unexpected="SECRET_UNFINISHED_STRING')
    with pytest.raises(ConfigurationError) as error:
        load_settings(str(config), command="status")
    assert "invalid TOML syntax" in str(error.value)
    assert "SECRET" not in str(error.value)


def test_ai_optional_partial_config_does_not_block_database_command(config, monkeypatch):
    environment(
        monkeypatch,
        {"EBKI_DATABASE_URL": ENV["EBKI_DATABASE_URL"], "EBKI_AI_URL": ENV["EBKI_AI_URL"]},
    )
    config.write_text(BUSINESS.replace('"rules_only"', '"ai"'))
    settings = load_settings(str(config), command="migrate")
    assert settings.ai_model is None


def test_doctor_reports_unchecked_mail_and_ai_connections(config, monkeypatch, capsys):
    import json

    environment(monkeypatch)
    monkeypatch.setattr("sys.argv", ["ebki", "--config", str(config), "doctor"])
    state = {"closed": False}
    runtime = SimpleNamespace(
        store=SimpleNamespace(one=lambda _: {"connected": True}),
        schema_version=2,
        ledger=SimpleNamespace(accounts=lambda: [], categories=lambda: []),
        close=lambda: state.update(closed=True),
    )
    monkeypatch.setattr(cli, "Runtime", lambda *a, **k: runtime)
    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["configuration_valid"] is True
    assert result["imap_connection"] == result["ai_connection"] == "not_checked"
    assert state["closed"] is True


@pytest.mark.parametrize(
    "replacement,field,reason",
    [
        ('timezone = "SECRET_TIMEZONE"', "timezone", "Asia/Shanghai"),
        ('classification_mode = "SECRET_MODE"', "classification_mode", "ai or rules_only"),
    ],
)
def test_business_validation_has_actionable_safe_reasons(
    config, monkeypatch, replacement, field, reason
):
    environment(monkeypatch)
    content = BUSINESS
    if replacement.startswith("timezone"):
        content = content.replace('timezone = "Asia/Shanghai"', replacement)
    elif replacement.startswith("classification_mode"):
        content = content.replace('classification_mode = "rules_only"', replacement)
    else:
        content = replacement + "\n" + content
    config.write_text(content)
    with pytest.raises(ConfigurationError) as error:
        load_settings(str(config), command="status")
    assert field in str(error.value) and reason in str(error.value)
    assert "SECRET" not in str(error.value)


def test_invalid_url_and_port_have_safe_actionable_reasons(config, monkeypatch):
    environment(monkeypatch)
    monkeypatch.setenv("EBKI_AI_URL", "INVALID-SECRET-URL")
    monkeypatch.setenv("EBKI_IMAP_PORT", "65536")
    with pytest.raises(ConfigurationError) as error:
        load_settings(str(config))
    message = str(error.value)
    assert "EBKI_AI_URL: must be an HTTP(S) base URL" in message
    assert "EBKI_IMAP_PORT: must be at most 65535" in message
    assert "SECRET" not in message


@pytest.mark.parametrize(
    "command", ["migrate", "status", "issues", "sync", "recheck", "run", "doctor"]
)
def test_runtime_database_creation_is_capability_controlled(
    config, monkeypatch, constructors, command
):
    environment(monkeypatch)
    runtime = bootstrap.Runtime(load_settings(str(config), command=command), command=command)
    kwargs = constructors.arguments["PostgresStore"][1]
    assert kwargs == ({"create_database": True} if command in {"migrate", "run"} else {})
    runtime.close()


@pytest.mark.parametrize("value", ["-1", "true", "1.5", '"7"'])
def test_rescan_days_requires_nonnegative_integer(config, monkeypatch, value):
    environment(monkeypatch)
    config.write_text(BUSINESS + f"rescan_days = {value}\n")
    with pytest.raises(ConfigurationError, match="mail.rescan_days"):
        load_settings(str(config))


@pytest.mark.parametrize("value", [0, 7, 14])
def test_rescan_days_business_configuration(config, monkeypatch, value):
    environment(monkeypatch)
    config.write_text(BUSINESS + f"rescan_days = {value}\n")
    assert load_settings(str(config)).mail.rescan_days == value


def test_console_dependency_profile_is_removed(config):
    with pytest.raises(ConfigurationError, match="unknown command"):
        load_settings(str(config), command="console")


@pytest.mark.parametrize("value", ["debug", "INFO", "warning", "ERROR", "critical"])
def test_log_level_toml_is_validated_and_normalized(config, monkeypatch, value):
    environment(monkeypatch, {"EBKI_DATABASE_URL": ENV["EBKI_DATABASE_URL"]})
    config.write_text(f'log_level = "{value}"\n' + BUSINESS)
    assert load_settings(str(config), command="status").log_level == value.upper()


@pytest.mark.parametrize("value", ['"SECRET_LEVEL"', "123", "true"])
def test_invalid_log_level_toml_reports_safe_field_name(config, monkeypatch, value):
    environment(monkeypatch, {"EBKI_DATABASE_URL": ENV["EBKI_DATABASE_URL"]})
    config.write_text(f"log_level = {value}\n" + BUSINESS)
    with pytest.raises(ConfigurationError) as error:
        load_settings(str(config), command="status")
    assert "log_level" in str(error.value)
    assert "SECRET_LEVEL" not in str(error.value)


def test_run_has_full_capabilities_and_required_configuration(config, monkeypatch):
    from ezbookkeeping_importer.config import command_capabilities

    assert command_capabilities("run", "ai") == {
        "database", "ledger", "mail", "ai", "evidence", "pipeline", "create_database"
    }
    with pytest.raises(ConfigurationError, match="EBKI_DATABASE_URL"):
        load_settings(str(config), command="run")
    environment(monkeypatch)
    assert load_settings(str(config)) == load_settings(str(config), command="run")
    with pytest.raises(ConfigurationError, match="unknown command"):
        load_settings(str(config), command="worker")


def test_recheck_cli_uses_local_dispatch_and_preserves_json(config, monkeypatch, capsys):
    import json

    environment(monkeypatch, {"EBKI_DATABASE_URL": ENV["EBKI_DATABASE_URL"]})
    monkeypatch.setattr("sys.argv", ["ebki", "--config", str(config), "recheck"])
    store = object()
    closed = []
    result = {"scheduled": 3, "already_pending": 2, "skipped": 1}
    runtime = SimpleNamespace(store=store, close=lambda: closed.append(True))
    monkeypatch.setattr(cli, "Runtime", lambda *a, **k: runtime)

    def schedule(actual_store, targets=None):
        assert targets is None
        assert actual_store is store
        return result

    monkeypatch.setattr(cli, "request_recheck", schedule)
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out) == result
    assert closed == [True]


def test_recheck_accepts_no_batch_bypass_options():
    parser = cli.build_parser()
    assert cli.parse_command(parser, ["recheck"]).command == "recheck"
    with pytest.raises(SystemExit) as error:
        cli.parse_command(parser, ["recheck", "--action", "confirm-new"])
    assert error.value.code == 2


def test_schema_failure_closes_store_before_other_service_construction(
    config, monkeypatch, constructors
):
    from ezbookkeeping_importer.domain.errors import DatabaseDiagnosticError

    environment(monkeypatch)
    def reject(self, **kwargs):
        raise DatabaseDiagnosticError('schema', 'schema_drift', '结构漂移')
    monkeypatch.setattr(Resource, 'migrate', reject)
    with pytest.raises(DatabaseDiagnosticError, match='schema_drift'):
        bootstrap.Runtime(load_settings(str(config)))
    assert set(constructors.created) == {'PostgresStore'}
    assert constructors.created['PostgresStore'].closed


def test_run_prepares_schema_with_stop_identity_and_safe_progress(
    config, monkeypatch, constructors, capsys
):
    import json
    from threading import Event
    from unittest.mock import Mock

    environment(monkeypatch)
    stopped = Event()
    migrate = Mock(return_value=2)
    monkeypatch.setattr(Resource, 'migrate', migrate)
    runtime = bootstrap.Runtime(load_settings(str(config)), stop_event=stopped)
    migrate.assert_called_once_with(stop_event=stopped, hold_worker=True,
                                   progress=bootstrap.startup_progress)
    assert constructors.arguments['PostgresStore'][1]['stop_event'] is stopped
    output = capsys.readouterr().out
    events = [json.loads(line) for line in output.splitlines()]
    assert [record['event'] for record in events] == ['database_preparing', 'database_ready']
    assert events[-1]['version'] == 2
    assert all(value not in output for value in ENV.values())
    runtime.close()


def test_migrate_does_not_print_startup_progress(config, monkeypatch, constructors, capsys):
    environment(monkeypatch)
    runtime = bootstrap.Runtime(load_settings(str(config), command='migrate'), command='migrate')
    assert runtime.schema_version == 2
    assert capsys.readouterr().out == ''
    runtime.close()
