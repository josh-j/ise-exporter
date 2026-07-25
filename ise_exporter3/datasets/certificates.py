"""System and trusted-store certificate expiry.

OpenAPI only. Unlike v2 this fetches its own node list rather than reading a
cache the deployment collector filled: one more request, but no hidden coupling
between datasets, and the request is declared in the cost rather than concealed.

Expiry thresholds are cumulative -- a certificate expiring in ten days counts in
the 30, 60 and 90 day buckets -- which matches how "expiring within N days" is
read on a dashboard.
"""
import logging
from datetime import datetime, timezone

from prometheus_client import Gauge

from ..compatibility import (
    MAX_CERTIFICATE_ROWS,
    MAX_CERTIFICATES_PER_STORE,
    validated_nodes,
)
from ..labels import label
from ..model import Cost, Dataset, Provider
from ..parsing import parse_ise_date
from ..transports import TransportError


logger = logging.getLogger(__name__)


expiry_days = Gauge(
    "ise3_certificate_expiry_days", "Days until a certificate expires",
    ["provider", "node", "certificate", "store", "usage"])
expiring_soon = Gauge(
    "ise3_certificates_expiring_soon",
    "Certificates expiring within the threshold", ["provider", "threshold_days"])
expired = Gauge(
    "ise3_certificates_expired", "Certificates already expired", ["provider"])
nodes_scanned = Gauge(
    "ise3_certificate_nodes_scanned",
    "Deployment nodes whose certificate store was read", ["provider"])
nodes_unreachable = Gauge(
    "ise3_certificate_nodes_unreachable",
    "Deployment nodes the PAN could not read certificates from. The PAN proxies "
    "this per node, so a node the PAN cannot resolve or reach fails only its own "
    "store", ["provider"])

_METRICS = (expiry_days, expiring_soon, expired, nodes_scanned, nodes_unreachable)

_THRESHOLDS = (30, 60, 90)


def _certificate_row(ctx, certificate, node, store, seen):
    if not isinstance(certificate, dict):
        ctx.fail("invalid_response", f"malformed {store} certificate response")
    expires = parse_ise_date(certificate.get("expirationDate", ""))
    if not expires:
        ctx.fail("invalid_response", f"{store} certificate had an invalid expirationDate")
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    # Not exported, but validated: a malformed value here suggests the whole
    # payload is untrustworthy, not just this field.
    if not isinstance(certificate.get("selfSigned", False), bool):
        ctx.fail("invalid_response", f"{store} certificate had an invalid selfSigned flag")
    try:
        key_size = int(certificate.get("keySize") or 0)
    except (TypeError, ValueError):
        ctx.fail("invalid_response", f"{store} certificate had an invalid keySize")
    if not 0 <= key_size <= 65_536:
        ctx.fail("invalid_response", f"{store} certificate had an invalid keySize")

    name = str(certificate.get("friendlyName") or certificate.get("id") or "").strip()
    if not name:
        ctx.fail("invalid_response", f"{store} certificate had no identity")
    identity = (label(node), label(name), store)
    if identity in seen:
        ctx.fail("invalid_response", "certificate inventory had a duplicate identity")
    seen.add(identity)
    return {
        "node": identity[0],
        "certificate": identity[1],
        "store": store,
        "usage": label(certificate.get("usedBy", certificate.get("trustedFor", "unknown"))),
        "days": (expires - datetime.now(timezone.utc)).days,
    }


def fetch(ctx):
    nodes = validated_nodes(
        ctx.transport.get_openapi("/deployment/node", api="pan_nodes"))
    if not nodes:
        ctx.fail("invalid_response", "no deployment node list for the certificate scan")

    rows, seen = [], set()
    scanned = unreachable = 0
    for node in nodes:
        hostname = node.get("hostname")
        try:
            certificates = ctx.transport.get_openapi_all(
                f"/certs/system-certificate/{hostname}", params={"size": 100},
                max_pages=10, max_rows=MAX_CERTIFICATES_PER_STORE,
                api="pan_sys_certs")
        except TransportError as error:
            # The PAN proxies this request to each node in turn, so one node it
            # cannot resolve or reach returns 422 for that node alone. Failing
            # the whole dataset would hide every other node's expiry dates over
            # one unreachable box -- exactly when you most want the others.
            unreachable += 1
            logger.warning(
                "certificate store unreadable for node %s: %s", hostname, error)
            continue
        scanned += 1
        if len(rows) + len(certificates) > MAX_CERTIFICATE_ROWS:
            ctx.fail("invalid_response", "certificate inventory exceeded the row ceiling")
        rows.extend(
            _certificate_row(ctx, certificate, hostname, "system", seen)
            for certificate in certificates)

    if not scanned and nodes:
        # Every node unreadable is a real outage, not partial coverage.
        ctx.fail("connection_failed",
                 "no node certificate store could be read through the PAN")

    trusted = ctx.transport.get_openapi_all(
        "/certs/trusted-certificate", params={"size": 100}, max_pages=10,
        max_rows=MAX_CERTIFICATES_PER_STORE, api="pan_trusted_certs")
    if len(rows) + len(trusted) > MAX_CERTIFICATE_ROWS:
        ctx.fail("invalid_response", "certificate inventory exceeded the row ceiling")
    rows.extend(
        _certificate_row(ctx, certificate, "trust_store", "trusted", seen)
        for certificate in trusted)

    counts = {threshold: 0 for threshold in _THRESHOLDS}
    already_expired = 0
    for row in rows:
        ctx.set(expiry_days, row["days"], node=row["node"],
                certificate=row["certificate"], store=row["store"], usage=row["usage"])
        if row["days"] < 0:
            already_expired += 1
            continue
        for threshold in _THRESHOLDS:
            if row["days"] <= threshold:
                counts[threshold] += 1
    for threshold in _THRESHOLDS:
        ctx.set(expiring_soon, counts[threshold], threshold_days=str(threshold))
    ctx.set(expired, already_expired)
    ctx.set(nodes_scanned, scanned)
    ctx.set(nodes_unreachable, unreachable)


DATASET = Dataset(
    name="certificates",
    description="System and trusted-store certificate expiry",
    default_interval=21600,
    metrics=_METRICS,
    providers=(
        Provider(
            name="openapi",
            # One node list, one system-certificate read per node, one trusted
            # store. Estimated for a four-node deployment; a larger one shows up
            # as planned-versus-measured drift rather than as a silent overrun.
            cost=Cost(target="pan", requests=6),
            notes="the PAN proxies each node's certificate store, so a node it "
                  "cannot resolve is reported through nodes_unreachable rather "
                  "than failing the dataset",
            supplies=frozenset({"expiry", "store", "node"}),
            fetch=fetch,
        ),
    ),
)
