"""The plain-container setup (Dockerfile, docker-compose.yml, docker/entrypoint.sh, .env.example) must stay consistent
with the application and with the VS Code dev container.

Almost nothing here needs Docker: the files are parsed and cross-checked (ports, bind address, data folder, build
arguments, what .dockerignore lets into the build, line endings), so a later change of e.g. the default port cannot
silently break the container. Two groups use Docker where it is there and skip themselves where it is not:

* ``docker compose config`` - needs only the Docker CLI with the compose plugin (no daemon);
* an end-to-end run of the image (build, start, health, user, data folder) - opt-in, because it builds the image:
  ``RUN_DOCKER_TESTS=1 pytest tests/test_docker.py``.  DOCKER_TEST_BUILD_ARGS / DOCKER_TEST_RUN_ARGS add options to
  ``docker build`` / ``docker run`` (a proxy, say).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"
ENV_EXAMPLE = ROOT / ".env.example"
DEV_DOCKERFILE = ROOT / ".devcontainer" / "Dockerfile"
NEW_FILES = [DOCKERFILE, COMPOSE, ENTRYPOINT, ENV_EXAMPLE, ROOT / ".dockerignore"]


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def _dockerfile_env(text: str) -> dict[str, str]:
    """Every ``ENV KEY=value`` pair of the Dockerfile (continued lines included)."""
    env: dict[str, str] = {}
    for block in re.findall(r"^ENV\s+((?:.*\\\n)*.*)$", text, re.M):
        for key, value in re.findall(r"(\w+)=(\S+)", block.replace("\\\n", " ")):
            env[key] = value
    return env


def _compose_variables(text: str) -> dict[str, str]:
    """``${NAME:-default}`` of the compose file -> {NAME: default}."""
    return dict(re.findall(r"\$\{(\w+):-([^}]*)\}", text))


# ---- what .dockerignore lets into the build -------------------------------------------------------------------------
def _glob_to_regex(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def ignored(path: str) -> bool:
    """What the Docker build does with a file: the patterns are checked in order and the last one that matches the path
    or one of its parent folders decides; a leading ``!`` lets the path through again."""
    parts = path.split("/")
    candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    result = False
    for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        regex = _glob_to_regex(line[1:] if negated else line)
        if any(regex.match(c) for c in candidates):
            result = not negated
    return result


def _copy_sources(text: str) -> list[str]:
    sources: list[str] = []
    for line in re.findall(r"^COPY\s+(?:--\S+\s+)*(.+)$", text, re.M):
        *srcs, _target = line.split()
        sources += srcs
    return sources


def test_dockerignore_lets_through_everything_the_dockerfile_copies(dockerfile):
    sources = _copy_sources(dockerfile)
    assert {"app.py", "ibkr_tracker", ".streamlit/config.toml", "docker/entrypoint.sh", "requirements.txt"} <= set(sources)
    for src in sources:
        assert (ROOT / src).exists(), f"{src} does not exist"
        files = [ROOT / src] if (ROOT / src).is_file() else [
            f for f in (ROOT / src).rglob("*")
            if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc"]
        assert files, f"{src} is empty"
        for f in files:
            rel = f.relative_to(ROOT).as_posix()
            assert not ignored(rel), f"{rel} is copied by the Dockerfile but excluded by .dockerignore"


@pytest.mark.parametrize("path", [
    "requirements.txt", "requirements-dev.txt",                       # the dev container image needs these
    "app.py", "ibkr_tracker/__init__.py", "ibkr_tracker/excel/builder.py", ".streamlit/config.toml",
    "docker/entrypoint.sh"])
def test_dockerignore_lets_the_needed_files_through(path):
    assert not ignored(path)


@pytest.mark.parametrize("path", [
    "data/tracker.db", "data/output/IBKR_Portfolio_U24013787.xlsx",    # the portfolio and its workbooks
    "examples/statements/U24013787_20260101_20261023.csv", "examples/output/x.xlsx",
    ".env", ".git/config", ".venv/bin/python", "tests/test_docker.py", "README.md", "scripts/make_sample_workbook.py",
    ".streamlit/secrets.toml", ".devcontainer/Dockerfile", ".vscode/launch.json",
    "ibkr_tracker/__pycache__/x.cpython-312.pyc", "ibkr_tracker/excel/__pycache__/y.cpython-313.pyc", "app.pyc"])
def test_dockerignore_keeps_private_and_unneeded_files_out_of_the_build(path):
    assert ignored(path)


# ---- the image ------------------------------------------------------------------------------------------------------
def test_image_is_the_same_stack_as_the_dev_container(dockerfile):
    base = re.search(r"^FROM\s+(\S+)", dockerfile, re.M).group(1)
    assert base == re.search(r"^FROM\s+(\S+)", DEV_DOCKERFILE.read_text(encoding="utf-8"), re.M).group(1)
    assert "libreoffice-calc" in dockerfile                            # the workbook gets calculated values
    assert "requirements.txt" in dockerfile                            # the same dependencies as everywhere else


def test_image_runs_the_app_with_the_same_settings_as_the_other_launchers(dockerfile):
    env = _dockerfile_env(dockerfile)
    launcher = (ROOT / "run_app.sh").read_text(encoding="utf-8")
    default_port = re.search(r"IBKR_TRACKER_PORT:-(\d+)", launcher).group(1)
    assert env["STREAMLIT_SERVER_PORT"] == default_port == re.search(r"^EXPOSE\s+(\d+)", dockerfile, re.M).group(1)
    assert env["IBKR_TRACKER_HOME"] == "/data"
    # .streamlit/config.toml keeps the app on 127.0.0.1 (no login!); inside the container it must listen on all
    # interfaces, or the published port could not reach it - the port mapping decides who may connect
    config = (ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    assert re.search(r'^address\s*=\s*"127\.0\.0\.1"', config, re.M)
    assert env["STREAMLIT_SERVER_ADDRESS"] == "0.0.0.0"
    # without a fixed address Streamlit looks up the external IP at start-up - 20+ seconds in a container without internet
    assert env["STREAMLIT_BROWSER_SERVER_ADDRESS"] == "localhost"
    assert re.search(r'^CMD\s+\["python", "-m", "streamlit", "run", "app\.py"\]', dockerfile, re.M)
    assert "--server" not in re.search(r"^CMD.*$", dockerfile, re.M).group(0)   # settings come from the environment
    assert re.search(r"^WORKDIR\s+/app$", dockerfile, re.M)


def test_image_starts_through_the_entrypoint_and_has_the_user_it_drops_to(dockerfile):
    assert re.search(r'^ENTRYPOINT\s+\["docker-entrypoint\.sh"\]', dockerfile, re.M)
    assert re.search(r"^COPY\s+docker/entrypoint\.sh\s+/usr/local/bin/docker-entrypoint\.sh", dockerfile, re.M)
    assert "chmod 0755 /usr/local/bin/docker-entrypoint.sh" in dockerfile      # a zip or Windows checkout may lose the bit
    assert "s/\\r$//" in dockerfile                                            # ... or turn the line endings into CRLF
    assert re.search(r"useradd\b.*\bapp\b", dockerfile) and "install -d -o app -g app /data" in dockerfile
    assert "id -g app" in ENTRYPOINT.read_text(encoding="utf-8")


def test_image_has_a_health_check_on_streamlits_health_endpoint(dockerfile):
    health = re.search(r"^HEALTHCHECK\b(?:[^\n]*\\\n)*[^\n]*", dockerfile, re.M).group(0)
    assert "/_stcore/health" in health and "STREAMLIT_SERVER_PORT" in health
    assert "curl" not in health                                                # the image has no curl


def test_build_arguments_have_the_same_defaults_in_dockerfile_and_compose(dockerfile, compose):
    declared = dict(re.findall(r"^ARG\s+(\w+)=(\S+)", dockerfile, re.M))
    args_block = re.search(r"^[ \t]+args:\n((?:[ \t]+\w+:.*\n)+)", compose, re.M).group(1)
    in_compose = {}
    for key, var, default in re.findall(r"^\s+(\w+):\s*\$\{(\w+):-([^}]*)\}\s*$", args_block, re.M):
        assert key == var, "the build argument and the variable that feeds it should have the same name"
        in_compose[key] = default
    assert in_compose and set(in_compose) <= set(declared)
    for key, default in in_compose.items():
        assert declared[key] == default, key


# ---- docker-compose.yml -------------------------------------------------------------------------------------------
def test_compose_publishes_the_app_on_this_computer_only_by_default(compose, dockerfile):
    match = re.search(r'^\s+-\s*"\$\{IBKR_TRACKER_BIND:-([\d.]+)\}:\$\{IBKR_TRACKER_PORT:-(\d+)\}:(\d+)"', compose, re.M)
    assert match, "the port mapping has to be <bind>:<port>:<container port>"
    bind, port, container_port = match.groups()
    assert bind == "127.0.0.1"                                                 # the app has no login
    assert port == re.search(r"IBKR_TRACKER_PORT:-(\d+)", (ROOT / "run_app.sh").read_text(encoding="utf-8")).group(1)
    assert container_port == _dockerfile_env(dockerfile)["STREAMLIT_SERVER_PORT"]


def test_compose_keeps_the_data_in_the_folder_the_other_launchers_use(compose, dockerfile):
    assert re.search(r"^\s+-\s*\./data:/data\s*$", compose, re.M)              # <project>/data, like run_app.sh / dev container
    assert _dockerfile_env(dockerfile)["IBKR_TRACKER_HOME"] == "/data"
    assert "output_dir" not in compose                                         # the workbooks belong in /data/output


def test_compose_hardening_and_what_the_entrypoint_needs(compose):
    script = ENTRYPOINT.read_text(encoding="utf-8")
    assert "no-new-privileges:true" in compose
    assert re.search(r"^\s+cap_drop:\n\s+-\s*ALL\s*$", compose, re.M)
    added = set(re.findall(r"^\s+-\s*([A-Z_]+)\s*$", re.search(r"cap_add:\n((?:\s+-\s*[A-Z_]+\n)+)", compose).group(1), re.M))
    assert added == {"CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID"}              # chown + setpriv, nothing else
    assert "setpriv" in script and "chown" in script
    assert re.search(r"^\s+init:\s*true\s*$", compose, re.M)
    assert re.search(r"^\s+restart:\s*unless-stopped\s*$", compose, re.M)
    assert re.search(r"^\s+TZ:\s*\$\{TZ:-UTC\}\s*$", compose, re.M)


def test_env_example_documents_every_setting_of_the_compose_file(compose):
    documented = dict(re.findall(r"^#?([A-Z][A-Z0-9_]*)=(.*)$", ENV_EXAMPLE.read_text(encoding="utf-8"), re.M))
    assert documented == _compose_variables(compose)
    assert ".env" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()    # personal settings are not committed
    assert ignored(".env") and ignored(".env.example")                                    # and neither goes into the image


def test_readme_documents_the_container_run():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for needle in ("docker compose up --build", "docker compose down", "http://localhost:8501", ".env.example",
                   "./data", "docker/entrypoint.sh", "Dev Containers: Reopen in Container"):
        assert needle in readme, needle
    for variable in _compose_variables(COMPOSE.read_text(encoding="utf-8")):
        assert variable in readme, f"{variable} is not explained in the README"


# ---- scripts and line endings --------------------------------------------------------------------------------------
def test_new_files_keep_lf_line_endings():
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    for pattern in (r"Dockerfile", r"docker-compose\.yml", r"\.env\.example", r"docker/\*\*", r"\*\.sh"):
        assert re.search(rf"^{pattern}\s+text\s+eol=lf", attributes, re.M), pattern
    for path in NEW_FILES:
        assert b"\r" not in path.read_bytes(), f"{path.name} has CRLF line endings"
    assert ENTRYPOINT.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash\n")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_entrypoint_parses_and_fails_on_errors():
    result = subprocess.run(["bash", "-n", str(ENTRYPOINT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert re.search(r"^set -eu$", ENTRYPOINT.read_text(encoding="utf-8"), re.M)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_entrypoint_passes_shellcheck():
    result = subprocess.run(["shellcheck", "--severity=warning", str(ENTRYPOINT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


@pytest.mark.skipif(shutil.which("bash") is None or sys.platform.startswith("win"), reason="needs bash on Linux/macOS")
def test_entrypoint_runs_the_command_unchanged_when_it_is_not_root():
    """Started as an ordinary user (docker run --user ..., or on a host where the script is tried out) the entrypoint only
    makes sure that there is a writable home folder and then runs the command - with all its arguments."""
    if os.getuid() == 0:
        pytest.skip("the root branch (drop privileges) is covered by the end-to-end tests")
    result = subprocess.run(["bash", str(ENTRYPOINT), "sh", "-c", 'echo "$0|$1|$HOME"', "one", "two words"],
                            capture_output=True, text=True, env={"PATH": os.environ["PATH"], "HOME": "/nonexistent"})
    assert result.returncode == 0, result.stderr
    first, second, home = result.stdout.strip().split("|")
    assert (first, second) == ("one", "two words")
    assert home == "/tmp"                                                      # /nonexistent is not writable


# ---- docker compose config (needs the Docker CLI, no daemon) -------------------------------------------------------
def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _compose_config(**overrides: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("TZ", "IBKR_TRACKER_PORT", "IBKR_TRACKER_BIND", "INSTALL_LIBREOFFICE", "APP_UID", "APP_GID")}
    env.update(overrides)
    result = subprocess.run(["docker", "compose", "-f", str(COMPOSE), "--project-directory", str(ROOT),
                             "config", "--format", "json"], capture_output=True, text=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["services"]["tracker"]


needs_compose = pytest.mark.skipif(not _compose_available(), reason="the Docker CLI with the compose plugin is not installed")


@needs_compose
def test_docker_compose_accepts_the_file_and_resolves_the_defaults():
    service = _compose_config()
    (port,) = service["ports"]
    assert (port["host_ip"], str(port["published"]), port["target"]) == ("127.0.0.1", "8501", 8501)
    (volume,) = service["volumes"]
    assert volume["type"] == "bind" and volume["target"] == "/data" and Path(volume["source"]) == ROOT / "data"
    assert service["environment"]["TZ"] == "UTC"
    assert service["build"]["args"] == {"INSTALL_LIBREOFFICE": "true", "APP_UID": "1000", "APP_GID": "1000"}
    assert set(service["cap_add"]) == {"CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID"} and service["cap_drop"] == ["ALL"]
    assert service["init"] is True and service["image"] == "ibkr-portfolio-tracker:latest"


@needs_compose
def test_docker_compose_settings_of_the_env_file_take_effect():
    service = _compose_config(IBKR_TRACKER_PORT="9000", IBKR_TRACKER_BIND="0.0.0.0", TZ="Europe/Budapest",
                              INSTALL_LIBREOFFICE="false")
    (port,) = service["ports"]
    assert (port["host_ip"], str(port["published"]), port["target"]) == ("0.0.0.0", "9000", 8501)
    assert service["environment"]["TZ"] == "Europe/Budapest"
    assert service["build"]["args"]["INSTALL_LIBREOFFICE"] == "false"


# ---- end-to-end: build the image, run it (opt-in) -------------------------------------------------------------------
RUN_E2E = os.environ.get("RUN_DOCKER_TESTS") == "1" and shutil.which("docker") is not None
e2e = pytest.mark.skipif(not RUN_E2E, reason="opt-in: RUN_DOCKER_TESTS=1 builds and runs the image (needs a running Docker)")
HEALTH = ("import os, sys, urllib.request; "
          "sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ['STREAMLIT_SERVER_PORT'] + "
          "'/_stcore/health', timeout=4).read() == b'ok' else 1)")


# the user ids of the processes that run the app (the needle is split, so that this very command line does not match)
APP_UIDS = """
import os
needle = b'stream' + b'lit run'
for pid in filter(str.isdigit, os.listdir('/proc')):
    try:
        if needle in open(f'/proc/{pid}/cmdline', 'rb').read().replace(b'\\0', b' '):
            print(open(f'/proc/{pid}/status').read().split('Uid:')[1].split()[0])
    except OSError:
        pass
"""


def _docker(*args: str, check: bool = True, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=timeout)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def image() -> str:
    tag = "ibkr-portfolio-tracker:pytest"
    extra = shlex.split(os.environ.get("DOCKER_TEST_BUILD_ARGS", ""))
    result = _docker("build", "-t", tag, "--build-arg", "INSTALL_LIBREOFFICE=false", *extra, str(ROOT), check=False)
    assert result.returncode == 0, result.stderr[-3000:]
    return tag


def _run_args() -> list[str]:
    return shlex.split(os.environ.get("DOCKER_TEST_RUN_ARGS", ""))


def _owner_is_host_user(path: Path) -> bool:
    return not sys.platform.startswith("linux") or os.getuid() == 0 or path.stat().st_uid == os.getuid()


@e2e
def test_container_becomes_healthy_and_the_app_does_not_run_as_root(image, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    name, port = f"ibkr-pytest-{uuid.uuid4().hex[:8]}", _free_port()
    _docker("run", "-d", "--name", name, "-e", f"STREAMLIT_SERVER_PORT={port}", "-v", f"{data}:/data",
            "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE", "--cap-add", "SETUID",
            "--cap-add", "SETGID", "--security-opt", "no-new-privileges:true", *_run_args(), image)
    try:
        begin = time.time()
        while time.time() - begin < 120:
            if _docker("exec", name, "python", "-c", HEALTH, check=False).returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("the app did not answer on /_stcore/health:\n" + _docker("logs", name, check=False).stdout[-2000:])
        assert time.time() - begin < 60, "the app needed more than a minute to start (is it waiting for the internet?)"
        uids = _docker("exec", name, "python", "-c", APP_UIDS).stdout.split()
        assert uids and all(int(u) != 0 for u in uids), uids
        assert "running as uid" in _docker("logs", name, check=False).stdout
    finally:
        _docker("rm", "-f", name, check=False)


@e2e
def test_statement_import_in_the_container_writes_files_that_belong_to_the_host_user(image, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    script = ("from pathlib import Path; from ibkr_tracker.tracker import Tracker; t = Tracker(); "
              "f = next(Path('/statements').glob('*.csv')); r = t.import_statement(f.name, f.read_bytes()); "
              "print('IMPORT_OK' if r.ok else 'IMPORT_FAILED')")
    result = _docker("run", "--rm", "-v", f"{data}:/data", "-v", f"{ROOT / 'examples' / 'statements'}:/statements:ro",
                     *_run_args(), image, "python", "-c", script, check=False)
    assert "IMPORT_OK" in result.stdout, result.stdout + result.stderr
    assert (data / "tracker.db").is_file()
    assert _owner_is_host_user(data / "tracker.db")                            # not root, not a stranger's uid
    assert _owner_is_host_user(data)
