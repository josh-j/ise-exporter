"""Device Administration configuration: internal accounts and policy structure.

ERS/OpenAPI only -- this is configuration, not activity. Deliberately separate
from ``tacacs_activity``, which is Data Connect-owned: the two describe the same
feature but must never substitute for one another.

The expensive part is per-account detail. Account attributes change when someone
edits an account, so each is fetched once and cached, and the per-cycle budget
fills the cache rather than capping how many accounts are ever classified.

Hygiene signals are the point of this dataset: an account whose password never
expires, or that is enabled and has never been used, is a finding. Those are only
visible in the per-account detail, which is why the fan-out is worth paying for.
"""
from prometheus_client import Gauge

from .. import detail_cache
from ..labels import label
from ..model import Cost, Dataset, Provider


CACHE = "ers_tacacs_user"
CACHE_TTL_SECONDS = 7 * 86400
WARMUP_FETCHES_PER_CYCLE = 100
# Publishing a series per account is only reasonable because internal Device
# Admin accounts are a small, human-managed set.
MAX_ACCOUNTS = 1000

accounts_total = Gauge(
    "ise3_tacacs_internal_accounts", "Internal Device Admin accounts", ["provider"])
accounts_classified = Gauge(
    "ise3_tacacs_internal_accounts_classified",
    "Accounts whose detail is cached; compare with the total for coverage",
    ["provider"])
account_enabled = Gauge(
    "ise3_tacacs_internal_account_enabled", "Internal account is enabled",
    ["provider", "username"])
account_hygiene = Gauge(
    "ise3_tacacs_internal_account_hygiene_risk",
    "A hygiene risk on an internal Device Admin account",
    ["provider", "username", "risk"])
policy_sets = Gauge(
    "ise3_tacacs_policy_sets", "Device Admin policy sets", ["provider"])

_METRICS = (accounts_total, accounts_classified, account_enabled,
            account_hygiene, policy_sets)


def hygiene_risks(detail):
    """Named risks for one account. Absence of a risk is published as 0."""
    risks = {}
    info = detail.get("passwordInfo") or {}
    risks["password_never_expires"] = int(
        str(info.get("passwordNeverExpires", "")).lower() == "true")
    risks["disabled_account_retained"] = int(
        str(detail.get("enabled", "true")).lower() == "false")
    return risks


def warm(ctx, cache, accounts):
    outstanding = [account["id"] for account in accounts
                   if cache.get(account["id"]) is None]
    for account_id in outstanding[:WARMUP_FETCHES_PER_CYCLE]:
        try:
            raw = ctx.transport.get_ers(
                f"/config/internaluser/{account_id}", api="ers_tacacs_user")
        except Exception:       # noqa: BLE001 - one account must not fail the set
            cache.count("failed")
            continue
        detail = raw.get("InternalUser") if isinstance(raw, dict) else None
        if not isinstance(detail, dict):
            cache.count("empty")
            continue
        # Retain only the bounded hygiene facts -- never the password payload.
        cache.put(account_id, {
            "enabled": int(str(detail.get("enabled", "true")).lower() != "false"),
            "risks": hygiene_risks(detail),
        })
        cache.count("fetched")
    return max(0, len(outstanding) - WARMUP_FETCHES_PER_CYCLE)


def fetch(ctx):
    users = ctx.transport.get_ers(
        "/config/internaluser", params={"size": 100}, get_all=True,
        api="ers_tacacs_users")
    if users is None:
        ctx.fail("invalid_response", "the internal user enumeration failed")
    if len(users) > MAX_ACCOUNTS:
        ctx.fail("response_too_large",
                 f"{len(users)} internal accounts exceeds the {MAX_ACCOUNTS} ceiling")

    accounts, seen = [], set()
    for user in users:
        if not isinstance(user, dict):
            ctx.fail("invalid_response", "an internal user entry was not an object")
        user_id = str(user.get("id") or "").strip()
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        accounts.append({"id": user_id, "name": label(user.get("name"), "unknown")})

    cache = detail_cache.shared(CACHE, ttl_seconds=CACHE_TTL_SECONDS)
    cache.retain({account["id"] for account in accounts})
    outstanding = warm(ctx, cache, accounts)
    cache.publish(len(accounts), deferred_count=outstanding)

    classified = 0
    for account in accounts:
        detail = cache.get(account["id"])
        if detail is None:
            continue
        classified += 1
        ctx.set(account_enabled, detail["enabled"], username=account["name"])
        for risk, present in detail["risks"].items():
            ctx.set(account_hygiene, present, username=account["name"], risk=risk)

    ctx.set(accounts_total, len(accounts))
    ctx.set(accounts_classified, classified)

    # Device Admin policy sets live on OpenAPI, not ERS -- the ERS path 404s on
    # 3.3 P11. Verified against the appliance.
    sets = ctx.transport.get_openapi(
        "/policy/device-admin/policy-set", api="pan_policy_sets")
    if isinstance(sets, list):
        ctx.set(policy_sets, len(sets))


DATASET = Dataset(
    name="tacacs_config",
    description="Device Admin internal accounts and policy configuration",
    default_interval=21600,
    metrics=_METRICS,
    providers=(
        Provider(
            name="ers",
            # Enumeration is complete each cycle; per-account detail is capped
            # per cycle behind a long cache, so classification converges.
            cost=Cost(target="pan", requests=25, scales_with="accounts",
                      warmup_requests=WARMUP_FETCHES_PER_CYCLE,
                      churn_fraction=0.0005),
            supplies=frozenset({"account", "policy_set", "rule_count", "hygiene"}),
            coverage="converging",
            fetch=fetch,
        ),
    ),
)
