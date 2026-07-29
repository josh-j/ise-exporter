#!/usr/bin/env bash
# Remove a v2 (ise-exporter) deployment completely.
#
# Usage:
#   sudo ./deploy/uninstall-v2.sh [--dry-run] [--yes] [--force]
#
# install.sh deliberately leaves v2 in place when it stages v3, because a
# migration that cannot be rolled back is not a migration. This is the other
# half of that decision: the step an operator takes once v3 has proven itself
# and the rollback target is no longer worth its disk, its systemd unit, or the
# cleartext appliance passwords in its configuration.
#
# It removes everything v2 installed -- unit, program, configuration,
# certificates, state, CLI, PowerShell module, and the service account. After
# this there is no v2 to go back to.
#
# Nothing named ise-exporter3 is touched. Every path below is spelled out in
# full and matched exactly: v2 and v3 differ by one character, which is a poor
# margin to leave to a glob.
set -euo pipefail

SERVICE_NAME=ise-exporter
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
UNIT_DROPIN_DIR="/etc/systemd/system/${SERVICE_NAME}.service.d"
INSTALL_DIR=/opt/ise-exporter
CONFIG_DIR=/etc/ise-exporter
STATE_DIR=/var/lib/ise-exporter
CLI_LINK=/usr/local/bin/ise-cli
PWSH_MODULE_LINK=/usr/local/share/powershell/Modules/Ise.Cli/2.0.0
PWSH_MODULE_DIR=/usr/local/share/powershell/Modules/Ise.Cli
SERVICE_USER=ise-exporter

V3_SERVICE_NAME=ise-exporter3

DRY_RUN=0
ASSUME_YES=0
FORCE=0

usage() {
    sed -n '2,19p' "$0"
}

for argument in "$@"; do
    case "$argument" in
        --dry-run)  DRY_RUN=1 ;;
        --yes|-y)   ASSUME_YES=1 ;;
        --force)    FORCE=1 ;;
        --help|-h)  usage; exit 0 ;;
        *)          echo "unknown option: $argument" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ $DRY_RUN -eq 0 && $EUID -ne 0 ]]; then
    echo "must run as root: sudo $0 [--dry-run] [--yes] [--force]" >&2
    exit 1
fi

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '    would run:'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    "$@"
}

# --- what is actually here ---------------------------------------------------

PRESENT=()
note_present() {
    if [[ -e "$1" || -L "$1" ]]; then
        PRESENT+=("$1")
    fi
}

SERVICE_PRESENT=0
if [[ -f "$UNIT_PATH" ]] || systemctl cat "$SERVICE_NAME" >/dev/null 2>&1; then
    SERVICE_PRESENT=1
    PRESENT+=("${SERVICE_NAME}.service")
fi
note_present "$UNIT_PATH"
note_present "$UNIT_DROPIN_DIR"
note_present "$INSTALL_DIR"
note_present "$CONFIG_DIR"
note_present "$STATE_DIR"
note_present "$CLI_LINK"
note_present "$PWSH_MODULE_DIR"

USER_PRESENT=0
if id "$SERVICE_USER" &>/dev/null; then
    USER_PRESENT=1
    PRESENT+=("user/group $SERVICE_USER")
fi

if (( ${#PRESENT[@]} == 0 )); then
    echo "==> no v2 deployment found; nothing to remove"
    exit 0
fi

# --- do not strand this host without an exporter -----------------------------

# The whole point of keeping v2 was the rollback. Removing it while v3 is not
# actually running turns "we migrated" into "we have no exporter", and the
# operator finds out from a silent dashboard rather than from this script.
if [[ $FORCE -eq 0 ]]; then
    if ! systemctl is-active --quiet "$V3_SERVICE_NAME"; then
        echo "error: $V3_SERVICE_NAME is not active, so v2 is still this host's" >&2
        echo "       only exporter and removing it would leave none." >&2
        echo "       Start v3 first, or pass --force if that is genuinely intended." >&2
        exit 1
    fi
    echo "==> $V3_SERVICE_NAME is active; v2 is safe to retire"
else
    echo "==> --force: not checking whether $V3_SERVICE_NAME is running"
fi

# --- the plan ----------------------------------------------------------------

echo
echo "This will permanently remove:"
for item in "${PRESENT[@]}"; do
    echo "  - $item"
done
if [[ -f "$CONFIG_DIR/config.toml" ]]; then
    echo
    echo "  $CONFIG_DIR/config.toml holds v2's appliance passwords in cleartext."
    echo "  If v3 has not imported them, take a copy before continuing:"
    echo "      sudo ise-exporter3-set-passwords --import-v2 --no-restart"
fi
echo
echo "There is no rollback to v2 afterwards."
echo

if [[ $DRY_RUN -eq 1 ]]; then
    echo "==> dry run; nothing will be changed"
fi

if [[ $DRY_RUN -eq 0 && $ASSUME_YES -eq 0 ]]; then
    if [[ ! -r /dev/tty ]]; then
        echo "error: no terminal to confirm on; pass --yes to proceed unattended" >&2
        exit 1
    fi
    read -r -p "Type 'remove' to continue: " reply < /dev/tty
    if [[ "$reply" != "remove" ]]; then
        echo "==> aborted; nothing was changed"
        exit 1
    fi
fi

# --- removal -----------------------------------------------------------------

if [[ $SERVICE_PRESENT -eq 1 ]]; then
    echo "==> stopping and disabling ${SERVICE_NAME}.service"
    run systemctl disable --now "$SERVICE_NAME" || true
    run systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true
fi

for path in "$UNIT_PATH" "$UNIT_DROPIN_DIR"; do
    if [[ -e "$path" || -L "$path" ]]; then
        echo "==> removing $path"
        run rm -rf -- "$path"
    fi
done
if [[ $SERVICE_PRESENT -eq 1 ]]; then
    run systemctl daemon-reload
fi

# The module link points into $INSTALL_DIR, so it goes first; the versioned
# directory above it is only ours if nothing else left a module there.
if [[ -e "$PWSH_MODULE_LINK" || -L "$PWSH_MODULE_LINK" ]]; then
    echo "==> removing $PWSH_MODULE_LINK"
    run rm -rf -- "$PWSH_MODULE_LINK"
fi
if [[ -d "$PWSH_MODULE_DIR" ]]; then
    if [[ $DRY_RUN -eq 1 ]] \
            || [[ -z "$(find "$PWSH_MODULE_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
        echo "==> removing $PWSH_MODULE_DIR"
        run rmdir -- "$PWSH_MODULE_DIR" || true
    else
        echo "==> leaving $PWSH_MODULE_DIR (another module version is installed there)"
    fi
fi

for path in "$CLI_LINK" "$INSTALL_DIR" "$CONFIG_DIR" "$STATE_DIR"; do
    if [[ -e "$path" || -L "$path" ]]; then
        echo "==> removing $path"
        run rm -rf -- "$path"
    fi
done

if [[ $USER_PRESENT -eq 1 ]]; then
    echo "==> removing service account $SERVICE_USER"
    run userdel "$SERVICE_USER" || true
    if getent group "$SERVICE_USER" >/dev/null; then
        run groupdel "$SERVICE_USER" || true
    fi
fi

# --- what is left ------------------------------------------------------------

if [[ $DRY_RUN -eq 1 ]]; then
    echo
    echo "==> dry run complete; nothing was changed"
    exit 0
fi

REMAINING=()
for path in "$UNIT_PATH" "$UNIT_DROPIN_DIR" "$INSTALL_DIR" "$CONFIG_DIR" \
        "$STATE_DIR" "$CLI_LINK" "$PWSH_MODULE_LINK"; do
    if [[ -e "$path" || -L "$path" ]]; then
        REMAINING+=("$path")
    fi
done
if id "$SERVICE_USER" &>/dev/null; then
    REMAINING+=("user $SERVICE_USER")
fi

echo
if (( ${#REMAINING[@]} )); then
    echo "==> v2 removed, but these survived and need a look:"
    for item in "${REMAINING[@]}"; do
        echo "  - $item"
    done
    exit 1
fi

echo "==> v2 is gone"
if systemctl is-active --quiet "$V3_SERVICE_NAME"; then
    echo "==> $V3_SERVICE_NAME is running; port 9618 is served by v3"
else
    echo "==> WARNING: $V3_SERVICE_NAME is not running; this host has no exporter"
fi
