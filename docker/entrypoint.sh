#!/usr/bin/env bash
# Entrypoint of the IBKR Portfolio Tracker container (see ../Dockerfile and the README, section 11.2).
#
# The container starts as root for a moment - only to make the data folder usable - and then runs the app as an
# unprivileged user. The data folder (IBKR_TRACKER_HOME, /data) holds the statement archive (tracker.db) and the
# workbooks; docker-compose.yml mounts ./data of your computer there. Who may write into it depends on the host:
#
#   * Docker Desktop (Windows, macOS)  the folder is writable for everybody: the app runs as the user "app".
#   * Linux, ./data is your folder     the app runs as the OWNER of the folder, so the files it writes stay yours.
#   * Linux, Docker has just created   the folder belongs to root: it is handed over to the user "app".
#     ./data
#   * rootless Docker / Podman         "root" in the container already is you: nothing to drop, nothing to change.
#
# Started with an explicit user (docker run --user 1000:1000 ..., or "user:" in docker-compose.yml) the script only
# starts the app - the data folder must then be writable for that user.
set -eu

data="${IBKR_TRACKER_HOME:-/data}"

# true inside a user namespace (rootless Docker, Podman): uid 0 there is an ordinary user of the host
in_user_namespace() {
    local outside count
    read -r _ outside count < /proc/self/uid_map 2>/dev/null || return 1
    [ "$outside" != "0" ] || [ "$count" != "4294967295" ]
}

# writable_for UID GID DIR: can that user create (and delete) a file in DIR?
writable_for() {
    # shellcheck disable=SC2016  # the single quotes are deliberate: the inner sh expands $1 and $$, not this one
    setpriv --reuid="$1" --regid="$2" --clear-groups \
        sh -c 'f="$1/.write-test.$$"; : > "$f" && rm -f "$f"' sh "$3" 2>/dev/null
}

if [ "$(id -u)" = "0" ] && ! in_user_namespace; then
    app_uid="$(id -u app)"
    app_gid="$(id -g app)"
    run_uid="$app_uid"
    run_gid="$app_gid"
    warned=0
    mkdir -p "$data"

    if ! writable_for "$run_uid" "$run_gid" "$data"; then
        owner_uid="$(stat -c '%u' "$data")"
        owner_gid="$(stat -c '%g' "$data")"
        if [ "$owner_uid" != "0" ] && writable_for "$owner_uid" "$owner_gid" "$data"; then
            # the folder of somebody else (you, on a Linux host): write the files as its owner
            run_uid="$owner_uid"
            run_gid="$owner_gid"
        elif [ "$owner_uid" = "0" ] && chown "$app_uid:$app_gid" "$data" 2>/dev/null; then
            : # a folder that Docker created as root: now it belongs to the app user (the folder only, not its content)
        else
            echo "WARNING: $data is not writable (owner uid $owner_uid): the app will not be able to save anything." \
                 "Make the folder writable for uid $app_uid, start the container with --user <uid>:<gid>, or - on" \
                 "SELinux hosts (Fedora, RHEL) - mount it with the :z option." >&2
            warned=1
        fi
    fi

    if [ "$run_uid" = "$app_uid" ]; then
        export HOME=/home/app
    else
        # a user the image does not know: give it a home folder and a passwd entry, so every tool finds a name and a home
        export HOME="/tmp/home-$run_uid" USER="user$run_uid" LOGNAME="user$run_uid"
        mkdir -p "$HOME"
        chown "$run_uid:$run_gid" "$HOME"
        getent group "$run_gid" > /dev/null || echo "user$run_gid:x:$run_gid:" >> /etc/group
        getent passwd "$run_uid" > /dev/null || echo "user$run_uid:x:$run_uid:$run_gid::$HOME:/usr/sbin/nologin" >> /etc/passwd
    fi

    # a file the app cannot write (left behind by an earlier run as root, say) would make it fail later: say so now
    if [ "$warned" = "0" ]; then
        stuck="$(setpriv --reuid="$run_uid" --regid="$run_gid" --clear-groups \
                     find "$data" -maxdepth 2 ! -writable -print -quit 2>/dev/null || true)"
        if [ -n "$stuck" ]; then
            echo "WARNING: $stuck is not writable for uid $run_uid - fix it with: sudo chown -R $run_uid:$run_gid <your data folder>" >&2
        fi
    fi

    echo "IBKR Portfolio Tracker: data folder $data, running as uid $run_uid (gid $run_gid)"
    exec setpriv --reuid="$run_uid" --regid="$run_gid" --clear-groups "$@"
fi

# started as an ordinary user (docker run --user ...) or in a user namespace: there must be a writable home folder
if [ ! -w "${HOME:-/}" ]; then
    export HOME=/tmp
fi
exec "$@"
