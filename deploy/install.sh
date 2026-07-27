#!/usr/bin/env bash
# Idempotent native systemd install/upgrade for ise-exporter3.
#
# Usage:
#   sudo ./deploy/install.sh [path-to-repo-checkout]
#   sudo ./deploy/install.sh --migrate-v2 [path-to-repo-checkout]
#
# Existing v3 deployments update in place. A detected v2 deployment is left
# running unless --migrate-v2 explicitly requests a health-checked handoff.
set -euo pipefail

MIGRATE_V2=0
SOURCE_DIR=""
for argument in "$@"; do
    case "$argument" in
        --migrate-v2)
            MIGRATE_V2=1
            ;;
        --help|-h)
            sed -n '2,9p' "$0"
            exit 0
            ;;
        -*)
            echo "unknown option: $argument" >&2
            exit 2
            ;;
        *)
            if [[ -n "$SOURCE_DIR" ]]; then
                echo "only one source checkout may be supplied" >&2
                exit 2
            fi
            SOURCE_DIR="$argument"
            ;;
    esac
done
SOURCE_DIR="${SOURCE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

INSTALL_DIR=/opt/ise-exporter3
CONFIG_DIR=/etc/ise-exporter3
CERTS_DIR="$CONFIG_DIR/certs"
STATE_DIR=/var/lib/ise-exporter3
CONFIG_FILE="$CONFIG_DIR/config.toml"
CREDENTIALS_FILE="$CONFIG_DIR/credentials"
SERVICE_USER=ise-exporter3
SERVICE_NAME=ise-exporter3
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
CLI_LINK=/usr/local/bin/ise-cli3
PWSH_DIR="$INSTALL_DIR/powershell"
PWSH_MODULE_LINK=/usr/local/share/powershell/Modules/Ise.Cli3/3.0.0
PASSWORD_TOOL="$INSTALL_DIR/set-passwords"
PASSWORD_LINK=/usr/local/sbin/ise-exporter3-set-passwords
VENV="$INSTALL_DIR/.venv"

V2_SERVICE_NAME=ise-exporter
V2_UNIT_PATH=/etc/systemd/system/ise-exporter.service
V2_CONFIG_FILE=/etc/ise-exporter/config.toml
V2_CERTS_DIR=/etc/ise-exporter/certs
V2_DETECTED=0
V2_WAS_ACTIVE=0
V2_WAS_ENABLED=0
SERVICE_INSTALLED_BEFORE=0
FRESH_CONFIG=0

if [[ -f "$UNIT_PATH" ]]; then
    SERVICE_INSTALLED_BEFORE=1
fi
if [[ -f "$V2_UNIT_PATH" ]] || systemctl cat "$V2_SERVICE_NAME" >/dev/null 2>&1; then
    V2_DETECTED=1
fi
if systemctl is-active --quiet "$V2_SERVICE_NAME"; then
    V2_WAS_ACTIVE=1
fi
if systemctl is-enabled --quiet "$V2_SERVICE_NAME"; then
    V2_WAS_ENABLED=1
fi

if [[ $EUID -ne 0 ]]; then
    echo "must run as root: sudo $0 [--migrate-v2] [path-to-repo-checkout]" >&2
    exit 1
fi
if [[ ! -f "$SOURCE_DIR/pyproject.toml" ]] \
        || [[ ! -f "$SOURCE_DIR/deploy/ise-exporter3.service" ]] \
        || [[ ! -f "$SOURCE_DIR/deploy/set-passwords.sh" ]]; then
    echo "error: $SOURCE_DIR is not an ise-exporter3 checkout" >&2
    exit 1
fi
if [[ "$MIGRATE_V2" -eq 1 && "$V2_DETECTED" -eq 0 ]]; then
    echo "error: --migrate-v2 requested but no $V2_SERVICE_NAME deployment was found" >&2
    exit 1
fi

echo "==> source: $SOURCE_DIR"
if [[ "$V2_DETECTED" -eq 1 ]]; then
    echo "==> detected existing v2 deployment ($V2_SERVICE_NAME.service)"
fi

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    echo "==> detected ${PRETTY_NAME:-unknown Linux distribution}"
fi

if command -v apt-get >/dev/null 2>&1 && command -v dpkg-query >/dev/null 2>&1; then
    REQUIRED_APT_PACKAGES=(python3 python3-venv ca-certificates)
    MISSING_APT_PACKAGES=()
    for package in "${REQUIRED_APT_PACKAGES[@]}"; do
        if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null \
                | grep -q 'install ok installed'; then
            MISSING_APT_PACKAGES+=("$package")
        fi
    done
    if (( ${#MISSING_APT_PACKAGES[@]} )); then
        echo "==> installing OS prerequisites: ${MISSING_APT_PACKAGES[*]}"
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        apt-get install -y --no-install-recommends "${MISSING_APT_PACKAGES[@]}"
    else
        echo "==> OS prerequisites already installed"
    fi
fi

for command_name in python3 useradd install systemctl readlink; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "error: required command not found: $command_name" >&2
        exit 1
    fi
done
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    echo "error: ise-exporter3 requires Python 3.10 or newer" >&2
    exit 1
fi

if ! id "$SERVICE_USER" &>/dev/null; then
    echo "==> creating system user $SERVICE_USER"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
else
    echo "==> user $SERVICE_USER already exists"
fi

echo "==> ensuring directories"
install -d -o root -g root -m 755 "$INSTALL_DIR"
install -d -o root -g "$SERVICE_USER" -m 750 "$CONFIG_DIR" "$CERTS_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 750 "$STATE_DIR"

# Reuse trusted CA material when staging beside v2, but never overwrite a v3
# certificate. Configuration still needs its paths reviewed because schemas
# and target names differ between major versions.
if [[ "$V2_DETECTED" -eq 1 && -d "$V2_CERTS_DIR" ]] \
        && [[ -z "$(find "$CERTS_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    echo "==> copying existing v2 CA material into the v3 certificate directory"
    cp -a "$V2_CERTS_DIR/." "$CERTS_DIR/"
fi

if [[ -d "$VENV" ]] && ! "$VENV/bin/python" -c 'import sys' &>/dev/null; then
    echo "==> broken or incompatible venv found; recreating $VENV"
    rm -rf -- "$VENV"
fi
if [[ ! -d "$VENV" ]]; then
    echo "==> creating venv at $VENV"
    python3 -m venv "$VENV"
else
    echo "==> reusing venv at $VENV"
fi

echo "==> installing/upgrading ise-exporter3"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q --upgrade "$SOURCE_DIR"
chown -R root:"$SERVICE_USER" "$VENV"
chmod -R go-w "$VENV"
chmod -R a+rX "$VENV"
INSTALLED_VERSION="$("$VENV/bin/python" -c \
    "import importlib.metadata as m; print(m.version('ise-exporter3'))")"
"$VENV/bin/ise-exporter3" --version
echo "==> installed ise-exporter3 $INSTALLED_VERSION"

echo "==> installing PowerShell operator CLI"
rm -rf -- "$PWSH_DIR"
install -d -o root -g root -m 755 "$PWSH_DIR"
cp -a "$SOURCE_DIR/powershell/." "$PWSH_DIR/"
chown -R root:root "$PWSH_DIR"
chmod -R go-w "$PWSH_DIR"
chmod -R a+rX "$PWSH_DIR"
# `a+rX` deliberately does not invent executable bits on regular data files.
# The launcher is code, though, and older checkouts accidentally committed it as
# 0644. Set it explicitly so an update repairs that deployment before the
# self-check executes it below.
chmod 755 "$PWSH_DIR/ise-cli3"
install -d -o root -g root -m 755 "$(dirname "$PWSH_MODULE_LINK")"
rm -rf -- "$PWSH_MODULE_LINK"
ln -s "$PWSH_DIR/Ise.Cli3" "$PWSH_MODULE_LINK"
install -d -o root -g root -m 755 "$(dirname "$CLI_LINK")"
ln -sfn "$PWSH_DIR/ise-cli3" "$CLI_LINK"
if [[ ! -r "$PWSH_DIR/Ise.Cli3.Profile.ps1" ]] \
        || [[ ! -r "$PWSH_DIR/Ise.Cli3/Ise.Cli3.psd1" ]]; then
    echo "error: installed Ise.Cli3 module self-check failed" >&2
    exit 1
fi
if command -v pwsh >/dev/null 2>&1; then
    "$CLI_LINK" 'Get-IseApiRoot' >/dev/null
    echo "==> verified ise-cli3"
else
    echo "==> WARNING: pwsh is not installed; exporter is ready but ise-cli3 is unavailable"
fi

echo "==> installing password-management helper"
install -o root -g root -m 755 "$SOURCE_DIR/deploy/set-passwords.sh" "$PASSWORD_TOOL"
install -d -o root -g root -m 755 "$(dirname "$PASSWORD_LINK")"
ln -sfn "$PASSWORD_TOOL" "$PASSWORD_LINK"

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "==> seeding $CONFIG_FILE"
    install -o root -g "$SERVICE_USER" -m 640 \
        "$SOURCE_DIR/ise-exporter3.toml.example" "$CONFIG_FILE"
    FRESH_CONFIG=1
else
    echo "==> preserving existing $CONFIG_FILE"
    chown root:"$SERVICE_USER" "$CONFIG_FILE"
    chmod 640 "$CONFIG_FILE"
fi
if [[ ! -f "$CREDENTIALS_FILE" ]]; then
    echo "==> seeding root-only $CREDENTIALS_FILE"
    install -o root -g root -m 600 /dev/null "$CREDENTIALS_FILE"
else
    echo "==> preserving existing root-only credentials"
    chown root:root "$CREDENTIALS_FILE"
    chmod 600 "$CREDENTIALS_FILE"
fi

chown root:"$SERVICE_USER" "$CERTS_DIR"
chmod 750 "$CERTS_DIR"
if compgen -G "$CERTS_DIR"'/*' >/dev/null; then
    chown root:"$SERVICE_USER" "$CERTS_DIR"/*
    chmod 640 "$CERTS_DIR"/*
    chmod 644 "$CERTS_DIR"/*.cer "$CERTS_DIR"/*.crt 2>/dev/null || true
fi

echo "==> installing systemd unit"
install -o root -g root -m 644 \
    "$SOURCE_DIR/deploy/ise-exporter3.service" "$UNIT_PATH"
systemctl daemon-reload

CREDENTIALS_READY=0
if "$PASSWORD_TOOL" --check >/dev/null 2>&1; then
    CREDENTIALS_READY=1
elif [[ "$MIGRATE_V2" -eq 1 && -f "$V2_CONFIG_FILE" ]]; then
    echo "==> attempting a non-printing import of existing v2 passwords"
    "$PASSWORD_TOOL" --import-v2 --no-restart || true
    if "$PASSWORD_TOOL" --check >/dev/null 2>&1; then
        CREDENTIALS_READY=1
    fi
fi

rollback_to_v2() {
    echo "==> v3 health check failed; rolling back service ownership to v2" >&2
    systemctl disable --now "$SERVICE_NAME" || true
    if [[ "$V2_WAS_ENABLED" -eq 1 ]]; then
        systemctl enable "$V2_SERVICE_NAME" || true
    fi
    if [[ "$V2_WAS_ACTIVE" -eq 1 ]]; then
        systemctl start "$V2_SERVICE_NAME" || true
    fi
}

verify_v3() {
    local _
    for _ in {1..20}; do
        if systemctl is-active --quiet "$SERVICE_NAME" \
                && "$VENV/bin/python" - "$CONFIG_FILE" >/dev/null 2>&1 <<'PY'
import json
import sys
import urllib.request

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

with open(sys.argv[1], "rb") as handle:
    exporter = (tomllib.load(handle).get("exporter") or {})
metrics_port = int(exporter.get("port", 9618))
api_port = int(exporter.get("api_port", 9619))
urllib.request.urlopen(
    f"http://127.0.0.1:{metrics_port}/metrics",
    timeout=2,
).read(1)
with urllib.request.urlopen(
    f"http://127.0.0.1:{api_port}/api/v1/health",
    timeout=2,
) as response:
    health = json.load(response)
if not health.get("fits_budget") or int(health.get("datasets_collecting") or 0) < 1:
    raise SystemExit(1)
PY
        then
            return 0
        fi
        sleep 1
    done
    return 1
}

if [[ "$MIGRATE_V2" -eq 1 ]]; then
    if [[ "$FRESH_CONFIG" -eq 1 || "$CREDENTIALS_READY" -eq 0 ]]; then
        echo "error: v3 configuration and credentials must validate before migration" >&2
        echo "edit $CONFIG_FILE, then run:" >&2
        echo "  sudo $PASSWORD_LINK --import-v2 --no-restart" >&2
        echo "  sudo $0 --migrate-v2 $SOURCE_DIR" >&2
        exit 1
    fi
    echo "==> handing port 9618 from v2 to v3"
    systemctl stop "$V2_SERVICE_NAME"
    systemctl disable "$V2_SERVICE_NAME"
    if ! systemctl enable --now "$SERVICE_NAME"; then
        rollback_to_v2
        exit 1
    fi
    if ! verify_v3; then
        systemctl --no-pager --lines=30 status "$SERVICE_NAME" || true
        rollback_to_v2
        exit 1
    fi
    echo "==> v3 is active, scrapeable, and collecting; v2 files were retained for rollback"
elif [[ "$V2_WAS_ACTIVE" -eq 1 || "$V2_WAS_ENABLED" -eq 1 ]]; then
    echo "==> v2 remains the enabled/running deployment; v3 will not claim port 9618"
    systemctl disable --now "$SERVICE_NAME" || true
    echo "==> complete v3 config/credentials, then rerun with --migrate-v2"
elif [[ "$FRESH_CONFIG" -eq 1 || "$CREDENTIALS_READY" -eq 0 ]]; then
    echo "==> enabling $SERVICE_NAME without starting it (configuration required)"
    systemctl enable "$SERVICE_NAME"
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        systemctl stop "$SERVICE_NAME"
    fi
elif [[ "$SERVICE_INSTALLED_BEFORE" -eq 0 ]]; then
    echo "==> enabling and starting $SERVICE_NAME"
    systemctl enable --now "$SERVICE_NAME"
elif systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "==> restarting active $SERVICE_NAME after upgrade"
    systemctl restart "$SERVICE_NAME"
else
    echo "==> $SERVICE_NAME is inactive; preserving operator-selected stopped state"
fi

sleep 1
systemctl --no-pager --lines=0 status "$SERVICE_NAME" || true

echo
echo "==> done — installed ise-exporter3 $INSTALLED_VERSION"
echo "==> config:      $CONFIG_FILE"
echo "==> credentials: $CREDENTIALS_FILE (root-only)"
echo "==> set/rotate:  sudo $PASSWORD_LINK --start"
echo "==> logs:        journalctl -u $SERVICE_NAME -f"
echo "==> metrics:     curl --fail http://127.0.0.1:9618/metrics"
echo "==> operator:    $CLI_LINK"
if [[ "$V2_DETECTED" -eq 1 && "$MIGRATE_V2" -eq 0 ]]; then
    echo
    echo "Existing v2 deployment was preserved. To migrate:"
    echo "  1. sudoedit $CONFIG_FILE"
    echo "  2. sudo $PASSWORD_LINK --import-v2 --no-restart"
    echo "  3. sudo $0 --migrate-v2 $SOURCE_DIR"
fi
if [[ "$FRESH_CONFIG" -eq 1 || "$CREDENTIALS_READY" -eq 0 ]]; then
    echo
    echo "Next steps:"
    echo "  1. sudoedit $CONFIG_FILE"
    echo "  2. install CA chains under $CERTS_DIR"
    echo "  3. sudo $PASSWORD_LINK --start"
    echo "  4. sudo systemctl status $SERVICE_NAME"
fi
