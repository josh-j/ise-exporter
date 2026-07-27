"""Secure loading of the root-only systemd EnvironmentFile.

systemd loads this file for the service, but an operator invoking
``ise-exporter3 plan`` directly does not pass through systemd.  This parser
accepts the deliberately small format written by ``set-passwords.sh`` without
turning the credentials file into a general shell script.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import stat


DEFAULT_CREDENTIALS_FILE = "/etc/ise-exporter3/credentials"
MAX_CREDENTIALS_BYTES = 64 * 1024
SECRET_KEYS = frozenset({
    "ISE_PASS",
    "ISE_DATACONNECT_PASSWORD",
    "ISE_PXGRID_PASSWORD",
})


class CredentialsError(ValueError):
    """A credentials file is missing, unsafe, or malformed."""


def load_credentials(path, *, environ=None, optional=False):
    """Return ``(environment, loaded_path)`` with explicit environment winning.

    Only the three exporter password variables are accepted.  The file must be
    a regular, non-symlink file owned by root or the invoking user and must not
    grant any group/other permissions.
    """
    existing = dict(os.environ if environ is None else environ)
    credentials_path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(credentials_path, flags)
    except FileNotFoundError:
        if optional:
            return existing, ""
        raise CredentialsError(
            f"credentials file does not exist: {credentials_path}") from None
    except PermissionError:
        if optional:
            return existing, ""
        raise CredentialsError(
            f"credentials file is not readable: {credentials_path}") from None
    except OSError as error:
        raise CredentialsError(
            f"cannot open credentials file {credentials_path}: {error}") from error

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CredentialsError(
                f"credentials file is not a regular file: {credentials_path}")
        if metadata.st_uid not in {0, os.geteuid()}:
            raise CredentialsError(
                f"credentials file must be owned by root or uid {os.geteuid()}: "
                f"{credentials_path}")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise CredentialsError(
                f"credentials file must not be accessible by group or others: "
                f"{credentials_path}")
        if metadata.st_size > MAX_CREDENTIALS_BYTES:
            raise CredentialsError(
                f"credentials file exceeds {MAX_CREDENTIALS_BYTES} bytes: "
                f"{credentials_path}")

        with os.fdopen(descriptor, encoding="utf-8") as stream:
            descriptor = -1
            lines = stream.read().splitlines()
    except UnicodeDecodeError as error:
        raise CredentialsError(
            f"credentials file is not UTF-8: {credentials_path}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    loaded = {}
    for number, raw in enumerate(lines, 1):
        try:
            tokens = shlex.split(raw, comments=True, posix=True)
        except ValueError as error:
            raise CredentialsError(
                f"invalid credentials file {credentials_path} line {number}: "
                f"{error}") from error
        if not tokens:
            continue
        if len(tokens) != 1 or "=" not in tokens[0]:
            raise CredentialsError(
                f"invalid credentials file {credentials_path} line {number}: "
                "expected KEY=VALUE")
        key, value = tokens[0].split("=", 1)
        if key not in SECRET_KEYS:
            raise CredentialsError(
                f"invalid credentials file {credentials_path} line {number}: "
                f"unsupported key {key!r}")
        if key in loaded:
            raise CredentialsError(
                f"invalid credentials file {credentials_path} line {number}: "
                f"duplicate key {key!r}")
        loaded[key] = value

    # A deliberately supplied process environment is the most specific source.
    merged = dict(loaded)
    merged.update(existing)
    return merged, str(credentials_path)
