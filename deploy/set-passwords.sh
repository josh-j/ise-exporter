#!/usr/bin/env bash
# Set or rotate the root-only environment credentials consumed by systemd.
# Passwords are read without echo and are never accepted as command arguments.
set -euo pipefail

CONFIG_DIR="${ISE_EXPORTER3_CONFIG_DIR:-/etc/ise-exporter3}"
CONFIG_FILE="$CONFIG_DIR/config.toml"
CREDENTIALS_FILE="$CONFIG_DIR/credentials"
V2_CONFIG_FILE="${ISE_EXPORTER_V2_CONFIG:-/etc/ise-exporter/config.toml}"
SERVICE_NAME=ise-exporter3
SERVICE_USER=ise-exporter3
ACTION=restart-active
MODE=prompt

usage() {
    cat <<'EOF'
Usage:
  sudo ise-exporter3-set-passwords [--start|--no-restart]
  sudo ise-exporter3-set-passwords --import-v2 [--start|--no-restart]
  sudo ise-exporter3-set-passwords --check

Interactive mode prompts without echo for:
  ISE_PASS                    PAN/MnT read-only account
  ISE_DATACONNECT_PASSWORD    Data Connect account
  ISE_PXGRID_PASSWORD         optional password-based pxGrid authentication

Blank input preserves an existing value. --import-v2 moves non-placeholder
passwords from /etc/ise-exporter/config.toml into the root-only v3 credentials
file without printing them. By default an active v3 service is restarted and an
inactive service stays stopped. --start enables and starts it after validation.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --start)
            ACTION=start
            ;;
        --no-restart)
            ACTION=none
            ;;
        --check)
            MODE=check
            ;;
        --import-v2)
            MODE=import-v2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ $EUID -ne 0 ]]; then
    echo "must run as root: sudo $0" >&2
    exit 1
fi

python_command() {
    if [[ -x /opt/ise-exporter3/.venv/bin/python ]]; then
        printf '%s\n' /opt/ise-exporter3/.venv/bin/python
    else
        printf '%s\n' python3
    fi
}

check_credentials() {
    local python
    python="$(python_command)"
    "$python" - "$CONFIG_FILE" "$CREDENTIALS_FILE" <<'PY'
import pathlib
import shlex
import sys

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

config_path = pathlib.Path(sys.argv[1])
credentials_path = pathlib.Path(sys.argv[2])
if not config_path.is_file():
    print(f"missing configuration: {config_path}", file=sys.stderr)
    raise SystemExit(1)
if not credentials_path.is_file():
    print(f"missing credentials: {credentials_path}", file=sys.stderr)
    raise SystemExit(1)

try:
    with config_path.open("rb") as handle:
        document = tomllib.load(handle)
except Exception as error:
    print(f"invalid configuration: {error}", file=sys.stderr)
    raise SystemExit(1)

allowed = {
    "ISE_PASS",
    "ISE_DATACONNECT_PASSWORD",
    "ISE_PXGRID_PASSWORD",
}
values = {}
try:
    for number, raw in enumerate(credentials_path.read_text().splitlines(), 1):
        tokens = shlex.split(raw, comments=True, posix=True)
        if not tokens:
            continue
        if len(tokens) != 1 or "=" not in tokens[0]:
            raise ValueError(f"line {number} is not KEY=VALUE")
        key, value = tokens[0].split("=", 1)
        if key not in allowed:
            raise ValueError(f"line {number} contains unsupported key {key!r}")
        values[key] = value
except Exception as error:
    print(f"invalid credentials file: {error}", file=sys.stderr)
    raise SystemExit(1)

targets = document.get("targets") or {}
required = []
pan = targets.get("pan") or {}
oracle = targets.get("oracle") or {}
pxgrid = targets.get("pxgrid") or {}
if str(pan.get("host") or "").strip():
    required.append("ISE_PASS")
if str(oracle.get("host") or "").strip():
    required.append("ISE_DATACONNECT_PASSWORD")
if (
    str(pxgrid.get("host") or "").strip()
    and not (
        str(pxgrid.get("client_cert") or "").strip()
        and str(pxgrid.get("client_key") or "").strip()
    )
):
    required.append("ISE_PXGRID_PASSWORD")

example_hosts = [
    f"targets.{name}.host"
    for name, target in targets.items()
    if str((target or {}).get("host") or "").lower().endswith(".example.com")
]
missing = [key for key in required if not values.get(key)]
if example_hosts:
    print(
        "replace example host values in config.toml: " + ", ".join(example_hosts),
        file=sys.stderr,
    )
if missing:
    print("missing required systemd credentials: " + ", ".join(missing), file=sys.stderr)
if example_hosts or missing:
    raise SystemExit(1)
print("systemd credentials and configured target hosts are ready")
PY
}

CONFIG_GROUP=root
if command -v getent >/dev/null 2>&1 \
        && getent group "$SERVICE_USER" >/dev/null; then
    CONFIG_GROUP="$SERVICE_USER"
fi
install -d -o root -g "$CONFIG_GROUP" -m 750 "$CONFIG_DIR"
if [[ ! -f "$CREDENTIALS_FILE" ]]; then
    install -o root -g root -m 600 /dev/null "$CREDENTIALS_FILE"
fi
chown root:root "$CREDENTIALS_FILE"
chmod 600 "$CREDENTIALS_FILE"

if [[ "$MODE" == check ]]; then
    check_credentials
    exit
fi

if [[ "$MODE" == import-v2 ]]; then
    if [[ ! -f "$V2_CONFIG_FILE" ]]; then
        echo "v2 configuration not found: $V2_CONFIG_FILE" >&2
        exit 1
    fi
    python="$(python_command)"
    "$python" - "$V2_CONFIG_FILE" "$CREDENTIALS_FILE" <<'PY'
import os
import pathlib
import shlex
import sys
import tempfile

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

v2_path = pathlib.Path(sys.argv[1])
destination = pathlib.Path(sys.argv[2])
with v2_path.open("rb") as handle:
    v2 = tomllib.load(handle)

allowed = (
    "ISE_PASS",
    "ISE_DATACONNECT_PASSWORD",
    "ISE_PXGRID_PASSWORD",
)
existing = {}
if destination.is_file():
    for raw in destination.read_text().splitlines():
        tokens = shlex.split(raw, comments=True, posix=True)
        if len(tokens) == 1 and "=" in tokens[0]:
            key, value = tokens[0].split("=", 1)
            if key in allowed:
                existing[key] = value

def nested(section, key):
    value = str((v2.get(section) or {}).get(key) or "")
    return "" if value.lower() == "changeme" else value

imported = {
    "ISE_PASS": nested("ise", "password"),
    "ISE_DATACONNECT_PASSWORD": nested("dataconnect", "password"),
    "ISE_PXGRID_PASSWORD": nested("pxgrid", "password"),
}
for key, value in imported.items():
    if value:
        existing[key] = value

def encoded(value):
    return value.replace("\\", "\\\\").replace('"', '\\"')

descriptor, temporary = tempfile.mkstemp(prefix=".credentials.", dir=destination.parent)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write("# Managed by ise-exporter3-set-passwords; root-only systemd environment.\n")
        for key in allowed:
            handle.write(f'{key}="{encoded(existing.get(key, ""))}"\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.chown(temporary, 0, 0)
    os.replace(temporary, destination)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise

names = [key for key, value in imported.items() if value]
print("imported v2 credentials: " + (", ".join(names) if names else "none"))
PY
else
    if [[ ! -t 0 ]]; then
        echo "interactive terminal required; passwords are not accepted as arguments" >&2
        exit 1
    fi

    prompt_secret() {
        local label="$1"
        local first second
        while true; do
            read -r -s -p "$label (blank keeps current): " first
            echo
            if [[ -z "$first" ]]; then
                REPLY_SECRET=""
                return
            fi
            if [[ "$first" == *$'\r'* || "$first" == *$'\n'* ]]; then
                echo "password may not contain a newline" >&2
                continue
            fi
            read -r -s -p "confirm $label: " second
            echo
            if [[ "$first" == "$second" ]]; then
                REPLY_SECRET="$first"
                return
            fi
            echo "values did not match; try again" >&2
        done
    }

    prompt_secret "PAN/MnT password (ISE_PASS)"
    PAN_PASSWORD="$REPLY_SECRET"
    prompt_secret "Data Connect password (ISE_DATACONNECT_PASSWORD)"
    DATACONNECT_PASSWORD="$REPLY_SECRET"
    prompt_secret "pxGrid password (ISE_PXGRID_PASSWORD, optional)"
    PXGRID_PASSWORD="$REPLY_SECRET"
    unset REPLY_SECRET

    write_credential() {
        local key="$1"
        local value="$2"
        local existing encoded
        if [[ -z "$value" ]]; then
            existing="$(grep -m1 -E "^${key}=" "$CREDENTIALS_FILE" || true)"
            if [[ -n "$existing" ]]; then
                printf '%s\n' "$existing"
                return
            fi
        fi
        encoded="${value//\\/\\\\}"
        encoded="${encoded//\"/\\\"}"
        printf '%s="%s"\n' "$key" "$encoded"
    }

    temporary="$(mktemp "$CONFIG_DIR/.credentials.XXXXXX")"
    cleanup() {
        rm -f -- "$temporary"
    }
    trap cleanup EXIT
    chmod 600 "$temporary"
    {
        echo "# Managed by ise-exporter3-set-passwords; root-only systemd environment."
        write_credential ISE_PASS "$PAN_PASSWORD"
        write_credential ISE_DATACONNECT_PASSWORD "$DATACONNECT_PASSWORD"
        write_credential ISE_PXGRID_PASSWORD "$PXGRID_PASSWORD"
    } >"$temporary"
    unset PAN_PASSWORD DATACONNECT_PASSWORD PXGRID_PASSWORD
    chown root:root "$temporary"
    mv -f -- "$temporary" "$CREDENTIALS_FILE"
    trap - EXIT
    echo "updated $CREDENTIALS_FILE (root:root, mode 0600)"
fi

if ! check_credentials; then
    echo "credentials were saved, but the service was not started" >&2
    exit 1
fi

case "$ACTION" in
    start)
        systemctl enable --now "$SERVICE_NAME"
        ;;
    restart-active)
        if systemctl is-active --quiet "$SERVICE_NAME"; then
            systemctl restart "$SERVICE_NAME"
        else
            echo "$SERVICE_NAME is inactive; preserving stopped state"
            echo "start it with: sudo systemctl enable --now $SERVICE_NAME"
        fi
        ;;
    none)
        echo "service lifecycle unchanged (--no-restart)"
        ;;
esac
