# IBKR Portfolio Tracker - runtime image: runs the app in a plain Docker container, no VS Code needed.
#
#   docker compose up --build                    (docker-compose.yml, see the README, section 11.2)
#   docker build -t ibkr-portfolio-tracker .     (then: docker run --rm -p 127.0.0.1:8501:8501 -v "$(pwd)/data:/data" ibkr-portfolio-tracker)
#
#   Ubuntu 24.04 LTS  +  system Python 3.12  +  LibreOffice Calc 24.2   = the stack the project is tested with
#   (the same as the VS Code dev container, which has its own image for development: .devcontainer/Dockerfile).
#
# The application code is part of the image - your data is not: the statement archive (tracker.db) and the workbooks
# live in /data, mount a folder of your computer there (docker-compose.yml mounts ./data).
# The build context is the project root; .dockerignore lets only the files below through - never the statements, the
# database in data/, .venv or .git.
FROM ubuntu:24.04

# the unprivileged user the app runs as (docker/entrypoint.sh decides who owns the data folder, see there)
ARG APP_UID=1000
ARG APP_GID=1000
# LibreOffice Calc (about 0.5 GB) lets the app store the calculated values inside the workbook, so that every viewer shows
# numbers. The app works without it - Excel then calculates the workbook when you open it.
# --build-arg INSTALL_LIBREOFFICE=false gives a much smaller image.
ARG INSTALL_LIBREOFFICE=true
ARG DEBIAN_FRONTEND=noninteractive

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:$PATH

# ---- operating-system packages ----------------------------------------------------------------------------------------
# ca-certificates: HTTPS to Yahoo Finance.  tzdata: the TZ variable (docker-compose.yml) needs the time zone database.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates python3 python3-venv tzdata \
 && if [ "$INSTALL_LIBREOFFICE" = "true" ]; then \
        apt-get install -y --no-install-recommends libreoffice-calc fonts-liberation; \
    fi \
 && rm -rf /var/lib/apt/lists/*

# ---- Python environment (own layer: rebuilt only when requirements.txt changes) ---------------------------------------
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm -f /tmp/requirements.txt

# ---- unprivileged user and the data folder (the Ubuntu image ships an "ubuntu" user with UID/GID 1000 - replace it) ----
RUN if id -u ubuntu >/dev/null 2>&1; then userdel --remove ubuntu; fi \
 && if getent group ubuntu >/dev/null; then groupdel ubuntu; fi \
 && groupadd --gid "$APP_GID" app \
 && useradd --uid "$APP_UID" --gid "$APP_GID" --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
 && install -d -o app -g app /data

# ---- the application (read-only for the app user: owned by root) ------------------------------------------------------
WORKDIR /app
COPY app.py ./
COPY ibkr_tracker ./ibkr_tracker
COPY .streamlit/config.toml ./.streamlit/config.toml
COPY docker/entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# (the sed makes the script work even when a Windows editor has turned its line endings into CRLF;
#  chmod: files that arrive with restrictive permissions must stay readable for the app user)
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh \
 && chmod 0755 /usr/local/bin/docker-entrypoint.sh \
 && chmod -R a+rX /app \
 && python -m compileall -q /app/ibkr_tracker

LABEL org.opencontainers.image.title="IBKR Portfolio Tracker" \
      org.opencontainers.image.description="Imports IBKR Activity Statements and builds a formula-driven Excel workbook (Streamlit app)"

# ---- run-time settings --------------------------------------------------------------------------------------------------
#   IBKR_TRACKER_HOME                  where the app keeps tracker.db and output/ (the folder to mount)
#   STREAMLIT_SERVER_ADDRESS/PORT      listen on all interfaces of the CONTAINER, otherwise the published port could not
#                                      reach the app (.streamlit/config.toml says 127.0.0.1); who can reach it from outside
#                                      is decided by the port mapping - docker-compose.yml publishes it on this computer only
#   STREAMLIT_BROWSER_SERVER_ADDRESS   without it Streamlit looks up the external IP address at start-up for its banner, and
#                                      a container without internet waits for that for 20 seconds or more
ENV IBKR_TRACKER_HOME=/data \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_BROWSER_SERVER_ADDRESS=localhost

# the app listens on 8501 inside the container; docker-compose.yml publishes it on this computer only
EXPOSE 8501

# Streamlit answers "ok" on /_stcore/health as soon as the app is up (python: the image has no curl)
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import os, sys, urllib.request; port = os.environ.get('STREAMLIT_SERVER_PORT', '8501'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + port + '/_stcore/health', timeout=4).read() == b'ok' else 1)"]

# The entrypoint starts as root only to make /data usable, then runs the command below as an unprivileged user.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "streamlit", "run", "app.py"]
