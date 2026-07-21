import json
from pathlib import Path


def test_registry_metadata_matches_project() -> None:
    root = Path(__file__).parents[1]
    metadata = json.loads((root / "server.json").read_text(encoding="utf-8"))
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert metadata["name"] == "io.github.namjeongwan/suparch"
    assert metadata["version"] == "0.6.0"
    assert metadata["packages"][0]["identifier"].endswith(":0.6.0")
    variables = {
        variable["name"]: variable
        for variable in metadata["packages"][0]["environmentVariables"]
    }
    assert "SUPARCH_CATALOG_POINTER_URL" not in variables
    assert variables["SUPARCH_CATALOG_URL"]["isRequired"] is True
    assert variables["SUPARCH_CATALOG_MANIFEST_URL"]["isRequired"] is False
    assert 'io.modelcontextprotocol.server.name="io.github.namjeongwan/suparch"' in dockerfile
    assert "mcp-name: io.github.namjeongwan/suparch" in readme
