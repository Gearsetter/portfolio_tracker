"""The dev container / VS Code configuration must stay consistent with the application itself.

Nothing here needs Docker or VS Code: the files are parsed and cross-checked (port, bind address, build context,
build arguments, line endings), so a later change of e.g. the default port cannot silently break the container.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEVC = ROOT / ".devcontainer"
VSCODE = ROOT / ".vscode"
JSON_FILES = [DEVC / "devcontainer.json", VSCODE / "launch.json", VSCODE / "tasks.json", VSCODE / "extensions.json"]


def load_jsonc(path: Path):
    """JSON with ``//`` and ``/* */`` comments - what VS Code and the dev container CLI accept (strings are left alone)."""
    text = path.read_text(encoding="utf-8")
    out: list[str] = []
    i, n, in_string = 0, len(text), False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\":
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            out.append(ch)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            i = text.index("*/", i) + 2
            continue
        else:
            out.append(ch)
        i += 1
    return json.loads("".join(out))


@pytest.fixture(scope="module")
def devcontainer():
    return load_jsonc(DEVC / "devcontainer.json")


@pytest.fixture(scope="module")
def dockerfile():
    return (DEVC / "Dockerfile").read_text(encoding="utf-8")


@pytest.mark.parametrize("path", JSON_FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_config_files_are_valid_jsonc(path):
    assert isinstance(load_jsonc(path), dict)


def test_port_and_bind_address_match_the_app(devcontainer):
    launcher = (ROOT / "run_app.sh").read_text(encoding="utf-8")
    default_port = int(re.search(r"IBKR_TRACKER_PORT:-(\d+)", launcher).group(1))
    assert devcontainer["forwardPorts"] == [default_port]
    assert str(default_port) in devcontainer["portsAttributes"]
    launch = load_jsonc(VSCODE / "launch.json")["configurations"][0]
    assert launch["args"][launch["args"].index("--server.port") + 1] == str(default_port)
    # the server is only reachable from inside the container; VS Code's port forwarding tunnels it to this computer
    assert re.search(r'^address\s*=\s*"127\.0\.0\.1"', (ROOT / ".streamlit" / "config.toml").read_text(), re.M)


def test_build_context_and_dockerignore_fit_the_dockerfile(devcontainer, dockerfile):
    build = devcontainer["build"]
    assert (DEVC / build["dockerfile"]).is_file()
    context = (DEVC / build["context"]).resolve()
    assert context == ROOT
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "*" in ignore                                      # whitelist: nothing but the listed files is sent
    for line in re.findall(r"^COPY\s+(?:--\S+\s+)*(.+)$", dockerfile, re.M):
        *sources, _target = line.split()
        for src in sources:
            assert (context / src).is_file(), src
            assert f"!{src}" in ignore, f"{src} is copied by the Dockerfile but not let through by .dockerignore"


def test_build_args_are_declared_in_the_dockerfile(devcontainer, dockerfile):
    declared = set(re.findall(r"^ARG\s+(\w+)", dockerfile, re.M))
    assert set(devcontainer["build"]["args"]) <= declared
    assert devcontainer["remoteUser"] in dockerfile             # the user the container is entered as is created there


def test_venv_location_is_consistent(devcontainer, dockerfile):
    venv = devcontainer["customizations"]["vscode"]["settings"]["python.defaultInterpreterPath"]
    assert venv == "/opt/venv/bin/python"
    assert "IBKR_TRACKER_VENV=/opt/venv" in dockerfile and "VIRTUAL_ENV=/opt/venv" in dockerfile
    assert "IBKR_TRACKER_VENV" in (ROOT / "run_app.sh").read_text(encoding="utf-8")    # run_app.sh honours it
    assert "requirements-dev.txt" in dockerfile                 # pytest etc. are in the image


def test_recommended_extensions_cover_the_container_ones(devcontainer):
    in_container = {e.lower() for e in devcontainer["customizations"]["vscode"]["extensions"]}
    recommended = {e.lower() for e in load_jsonc(VSCODE / "extensions.json")["recommendations"]}
    assert in_container <= recommended


def test_server_ready_pattern_matches_the_streamlit_banner():
    pattern = load_jsonc(VSCODE / "launch.json")["configurations"][0]["serverReadyAction"]["pattern"]
    for banner in ("  URL: http://127.0.0.1:8501", "  Local URL: http://localhost:8501"):
        match = re.search(pattern, banner)
        assert match and match.group(1) == "8501", banner


def test_linux_scripts_keep_lf_line_endings():
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"^\*\.sh\s+text\s+eol=lf", attributes, re.M)
    assert re.search(r"^\.devcontainer/\*\*\s+text\s+eol=lf", attributes, re.M)
    for script in (ROOT / "run_app.sh", DEVC / "post-create.sh", DEVC / "Dockerfile", DEVC / "devcontainer.json"):
        assert b"\r" not in script.read_bytes(), f"{script.name} has CRLF line endings"
    for script in (ROOT / "run_app.sh", DEVC / "post-create.sh"):
        assert script.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
@pytest.mark.parametrize("script", [ROOT / "run_app.sh", DEVC / "post-create.sh"], ids=lambda p: p.name)
def test_shell_scripts_parse(script):
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
