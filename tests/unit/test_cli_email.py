import pytest
from ezbookkeeping_importer.entrypoints import cli


def test_removed_import_eml_is_not_an_entrypoint(monkeypatch):
    monkeypatch.setattr("sys.argv", ["ebki", "import-eml", "synthetic.eml"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
