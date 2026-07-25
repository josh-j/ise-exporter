"""Endpoint hardware model, OS, and MDM attributes.

This is the data v2 lost. Data Connect has no equivalent: manufacturer, model and
MDM state are not columns in any reporting view, so removing pxGrid removed the
capability outright rather than moving it to another source. pxGrid getEndpoints
is the only ISE 3.3 interface that carries them.

**There is deliberately no fallback.** An ERS provider was tried and removed. It
supplied only `profile`, which ``endpoint_inventory`` already covers completely
from a single Data Connect query, and it could not converge: 100,000 endpoints at
one request each needs roughly 2,000 requests an hour merely to keep a cache
warm -- about two thirds of the entire PAN budget -- to duplicate something
cheap. A cache warm-up cannot rescue a provider that should not exist; a fallback
costing a thousand times more for a narrower answer is not a fallback.

So when pxGrid is unconfigured this dataset is unavailable and ``plan`` says so
by name. That is the honest statement: model, OS and MDM have no other source
here, and an empty panel that explains itself beats a full one that quietly
answers a different question.

The pxGrid directory is itself partial -- it carries only endpoints ISE is
actively publishing profiling context for, and it returned zero on both this lab
and production -- which is why its coverage is declared `bounded`.
"""
from prometheus_client import Gauge

from ..model import Cost, Dataset, Provider


by_model = Gauge(
    "ise3_endpoint_model", "Endpoints per hardware model",
    ["provider", "manufacturer", "model"])
by_os = Gauge(
    "ise3_endpoint_operating_system", "Endpoints per operating system",
    ["provider", "os"])
mdm_registered = Gauge(
    "ise3_endpoint_mdm_registered", "Endpoints by MDM registration state",
    ["provider", "registered"])
mdm_compliant = Gauge(
    "ise3_endpoint_mdm_compliant", "Endpoints by MDM compliance state",
    ["provider", "compliant"])
published = Gauge(
    "ise3_endpoint_attributes_published",
    "Endpoints ISE is publishing profiling context for. Compare with "
    "ise3_endpoints_total: the gap is what this source cannot see",
    ["provider"])

_METRICS = (by_model, by_os, mdm_registered, mdm_compliant, published)


DATASET = Dataset(
    name="endpoint_attributes",
    description="Endpoint model, OS, and MDM attributes",
    default_interval=21600,
    metrics=_METRICS,
    providers=(
        Provider(
            name="pxgrid",
            cost=Cost(target="pxgrid", requests=1),
            supplies=frozenset({"model", "manufacturer", "os", "mdm"}),
            coverage="bounded",
            requires=("capability:pxgrid_endpoints",),
            notes="only endpoints ISE is actively publishing profiling context "
                  "for; endpoints without live context are absent entirely, and "
                  "this returned zero on both this lab and production. No "
                  "fallback exists because no other ISE 3.3 interface carries "
                  "model, OS or MDM",
        ),
    ),
)
