import json
from pathlib import Path


def test_registry_metadata_matches_project() -> None:
    root = Path(__file__).parents[1]
    metadata = json.loads((root / "server.json").read_text(encoding="utf-8"))
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert metadata["name"] == "io.github.namjeongwan/suparch"
    assert metadata["version"] == "0.1.1"
    assert metadata["packages"][0]["identifier"].endswith(":0.1.1")
    assert 'io.modelcontextprotocol.server.name="io.github.namjeongwan/suparch"' in dockerfile
    assert "mcp-name: io.github.namjeongwan/suparch" in readme
