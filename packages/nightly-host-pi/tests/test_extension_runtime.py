"""Execute the extension against a hook that waits for stdin EOF."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nightly_host_pi import render_keepalive_ts


def test_extension_closes_hook_stdin_and_continues(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the TypeScript extension")
    probe = subprocess.run(
        [node, "--experimental-strip-types", "-e", ""], capture_output=True, check=False
    )
    if probe.returncode:
        pytest.skip("Node must support TypeScript type stripping")

    run = tmp_path / ".nightly" / "runs" / "test"
    run.mkdir(parents=True)
    (run.parent / "CURRENT").write_text("test")
    (run / "SESSION_ACTIVE").touch()
    binary = tmp_path / "nightly"
    binary.write_text(
        f"#!{Path(sys.executable).resolve()}\n"
        "import json, sys\n"
        "assert json.loads(sys.stdin.read()) == {}\n"
        "print(json.dumps({'deliver_as': 'followUp', 'message': 'continue-test'}))\n"
    )
    binary.chmod(0o755)
    (tmp_path / "extension.ts").write_text(render_keepalive_ts("test"))
    harness = tmp_path / "harness.mjs"
    harness.write_text(
        'import install from "./extension.ts";\n'
        "let handler;\n"
        "install({on(event, callback) { handler = callback; },\n"
        "sendMessage(message, options) { console.log(JSON.stringify({message, options})); }});\n"
        "await handler({}, {isIdle: () => true});\n"
    )
    result = subprocess.run(
        [node, "--experimental-strip-types", str(harness)],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["message"]["content"] == "continue-test"
    assert payload["options"] == {"deliverAs": "followUp", "triggerTurn": True}
