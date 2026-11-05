import asyncio
import json

from bench.phase0_spike import amain


def test_spike_dry_run(tmp_path, capsys):
    code = asyncio.run(
        amain(["--fake", "--settle", "0", "--repeats", "1", "--depth", "3", "--out", str(tmp_path)])
    )
    assert code == 0
    (result_file,) = tmp_path.glob("phase0-fake-*.json")
    data = json.loads(result_file.read_text())
    assert data["errors"] == [] and data["cleanup_errors"] == []
    assert data["delete_parent_with_live_fork"].startswith("refused")
    assert [f["n"] for f in data["fanout"]] == [2, 4, 8]
    assert "DRY RUN" in capsys.readouterr().out
