import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_environment_json_matches_cloud_agent_schema() -> None:
    payload = json.loads((ROOT / ".cursor" / "environment.json").read_text(encoding="utf-8"))
    assert payload["name"]
    assert payload["user"] == "ubuntu"
    assert payload["install"] == "bash .cursor/install.sh"
    assert {row["name"] for row in payload["terminals"]} == {"mixer-api", "gui"}
    ports = payload["ports"]
    assert all(isinstance(row, dict) and "port" in row for row in ports)
    assert {row["port"] for row in ports} == {9610, 9620}
