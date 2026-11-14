# Development image for the IBKR Portfolio Tracker (referenced by devcontainer.json).
#
#   Ubuntu 24.04 LTS  +  system Python 3.12  +  LibreOffice Calc 24.2   = the stack the project is tested with.
#
# The Python packages go into /opt/venv - deliberately outside the workspace, so that a Windows ".venv" in the
# project folder never collides with the Linux environment of the container.
# The build context is the project root, but .dockerignore lets only the two requirements files through.
FROM ubuntu:24.04

ARG USERNAME=vscode
ARG USER_UID=1000
ARG USER_GID=1000
# LibreOffice Calc (about 0.5 GB): lets the app store calculated values inside the workbook and enables the tests that
# recalculate the workbook.  The app and all other tests work without it.
ARG INSTALL_LIBREOFFICE=true
# Chromium for the real-browser smoke test (tests/test_gui.py::test_browser_smoke, about 0.7 GB).  Skipped when absent.
ARG INSTALL_PLAYWRIGHT=false
ARG DEBIAN_FRONTEND=noninteractive

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    IBKR_TRACKER_VENV=/opt/venv \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    PATH=/opt/venv/bin:$PATH

# ---- operating-system packages ----------------------------------------------------------------------------------------
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates curl git less openssh-client procps sudo unzip \
        python3 python3-venv \
 && if [ "$INSTALL_LIBREOFFICE" = "true" ]; then \
        apt-get install -y --no-install-recommends libreoffice-calc fonts-liberation; \
    fi \
 && rm -rf /var/lib/apt/lists/*

# ---- non-root user (the Ubuntu image ships an "ubuntu" user with UID/GID 1000 - replace it) -----------------------------
RUN if id -u ubuntu >/dev/null 2>&1; then userdel --remove ubuntu; fi \
 && if getent group ubuntu >/dev/null; then groupdel ubuntu; fi \
 && groupadd --gid "$USER_GID" "$USERNAME" \
 && useradd --uid "$USER_UID" --gid "$USER_GID" --create-home --shell /bin/bash "$USERNAME" \
 && echo "$USERNAME ALL=(root) NOPASSWD:ALL" > "/etc/sudoers.d/$USERNAME" \
 && chmod 0440 "/etc/sudoers.d/$USERNAME" \
 && install -d -o "$USERNAME" -g "$USER_GID" /opt/venv

# ---- Python environment -------------------------------------------------------------------------------------------------
USER $USERNAME
COPY --chown=$USERNAME:$USER_GID requirements.txt requirements-dev.txt /tmp/requirements/
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements/requirements-dev.txt \
 && if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then /opt/venv/bin/pip install --no-cache-dir "playwright>=1.45"; fi \
 && rm -rf /tmp/requirements

# ---- optional: Chromium for the browser smoke test (needs root for its system libraries) ---------------------------------
USER root
RUN if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
        playwright install --with-deps chromium \
     && chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH" \
     && rm -rf /var/lib/apt/lists/*; \
    fi

USER $USERNAME
