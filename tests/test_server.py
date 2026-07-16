import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from suparch.catalog import SQLiteCatalogBuilder, load_json_catalog
from suparch.server import mcp

SAMPLE_CATALOG = (
    Path(__file__).parents[1] / "src" / "suparch" / "data" / "sample_catalog.json"
)


def test_registers_expected_mcp_tools() -> None:
    tools = asyncio.run(mcp.list_tools())

    assert {tool.name for tool in tools} == {
        "search_products",
        "get_product",
        "get_catalog_info",
        "compare_products",
        "calculate_stack",
    }
    assert mcp.settings.stateless_http is True


def test_stdio_protocol_initialization() -> None:
    messages = "\n".join(
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "0.1"},
                    },
                }
            ),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                }
            ),
        ]
    )
    result = subprocess.run(
        [sys.executable, "-m", "suparch.server"],
        input=messages + "\n",
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )

    response = json.loads(result.stdout.splitlines()[0])
    assert response["result"]["serverInfo"]["name"] == "Suparch"
    assert response["result"]["serverInfo"]["version"] == "0.4.0"


def test_streamable_http_initialization(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite"
    SQLiteCatalogBuilder().build(load_json_catalog(SAMPLE_CATALOG), database)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    environment = {
        **os.environ,
        "SUPARCH_DB_PATH": str(database),
        "SUPARCH_TRANSPORT": "streamable-http",
        "SUPARCH_HOST": "127.0.0.1",
        "SUPARCH_PORT": str(port),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "suparch.server"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        response = None
        for _ in range(50):
            try:
                response = httpx.post(
                    f"http://127.0.0.1:{port}/mcp",
                    headers={"Accept": "application/json, text/event-stream"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "pytest", "version": "0.1"},
                        },
                    },
                    timeout=1,
                )
                break
            except httpx.ConnectError:
                time.sleep(0.1)
        assert response is not None
        assert response.status_code == 200
        assert response.json()["result"]["serverInfo"]["name"] == "Suparch"
    finally:
        process.terminate()
        process.wait(timeout=10)
