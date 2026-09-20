from datetime import date

import pytest

from ezbookkeeping_importer.entrypoints import cli


@pytest.mark.parametrize(
    "arguments",
    [
        ["--since", "2026-01-01"],
        ["--until", "2026-01-01"],
        ["--since", "2026-02-01", "--until", "2026-01-01"],
        ["--since", "2026-02-30", "--until", "2026-03-01"],
        ["--since", "20260101", "--until", "2026-01-02"],
        ["--since", "2026-01-01", "--until", "9999-12-31"],
    ],
)
def test_sync_range_rejected_before_configuration_or_connections(monkeypatch, arguments):
    monkeypatch.setattr("sys.argv", ["ebki", "sync", *arguments])
    monkeypatch.setattr(cli, "load_settings", lambda _: pytest.fail("invalid range opened config"))
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 2


def test_calendar_date_is_explicit():
    assert cli.calendar_date("2026-01-01") == date(2026, 1, 1)
