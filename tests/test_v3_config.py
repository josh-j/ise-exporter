"""Configuration is the clarity deliverable, so its rejections are part of the
contract: an operator must be told which key is wrong and what the valid choices
are, rather than getting a silently-defaulted value."""
import pytest

from ise_exporter3.config import (
    Config,
    ConfigError,
    format_duration,
    parse_duration,
)


def _document(**overrides):
    document = {
        "profile": "lab",
        "targets": {
            "pan": {"host": "pan1.example.com", "user": "ers.readonly"},
            "mnt": {"host": "mnt1.example.com"},
            "oracle": {"host": "mnt1.example.com", "user": "dataconnect",
                       "service": "cpm10"},
        },
    }
    document.update(overrides)
    return document


def _config(document=None, environ=None):
    return Config.from_document(
        document or _document(), path="test.toml",
        environ=environ if environ is not None else {
            "ISE_PASS": "pan-secret",
            "ISE_DATACONNECT_PASSWORD": "oracle-secret",
        })


@pytest.mark.parametrize("text,seconds", [
    ("30s", 30), ("5m", 300), ("6h", 21600), ("1d", 86400), ("45", 45), (900, 900),
])
def test_durations_accept_units_and_bare_seconds(text, seconds):
    assert parse_duration(text, key="k") == seconds


@pytest.mark.parametrize("value", ["", "5x", "-1", "m", 0, True])
def test_bad_durations_name_the_key(value):
    with pytest.raises(ConfigError, match="k"):
        parse_duration(value, key="k")


@pytest.mark.parametrize("seconds,text", [
    (300, "5m"), (21600, "6h"), (86400, "1d"), (45, "45s"), (90, "90s")])
def test_durations_round_trip_into_the_shortest_exact_unit(seconds, text):
    assert format_duration(seconds) == text


def test_the_default_profile_is_sized_for_the_production_fleet():
    # "Out of the box" means an operator who names no profile must get something
    # that works at ~90k endpoints / ~60k sessions / ~5k NADs. A profile that
    # under-declares the fleet makes every cost estimate optimistic, which is the
    # one direction the load model must not fail in.
    document = _document()
    document.pop("profile")
    config = _config(document)
    assert config.profile == "production"
    assert config.scale.endpoints == 90_000
    assert config.scale.nads == 5_000
    assert config.scale.sessions == 60_000
    assert config.scale.policy_sets == 100


def test_profile_supplies_scale_and_budget_defaults():
    small = _config()
    assert small.scale.endpoints == 5_000
    assert small.scale.policy_sets == 20
    assert small.budget_for("oracle").duty_cycle_percent == pytest.approx(1.0)

    large = _config(_document(profile="production"))
    assert large.scale.endpoints == 90_000
    assert large.budget_for("oracle").duty_cycle_percent == pytest.approx(3.0)


def test_explicit_values_override_the_profile():
    config = _config(_document(
        profile="production",
        scale={"endpoints": 250_000},
        budget={"oracle": {"duty_cycle_percent": 0.5}}))
    assert config.scale.endpoints == 250_000
    assert config.scale.nads == 5_000          # untouched profile default
    assert config.budget_for("oracle").duty_cycle_percent == pytest.approx(0.5)


def test_secrets_come_from_the_environment_only():
    config = _config(environ={"ISE_PASS": "from-env"})
    assert config.target("pan").password == "from-env"
    # No TOML key exists for a password at all.
    with pytest.raises(ConfigError, match="password"):
        _config(_document(targets={"pan": {"host": "p", "password": "in-file"}}))


def test_mnt_reuses_the_pan_account_rather_than_a_second_credential():
    config = _config()
    assert config.target("mnt").user == "ers.readonly"
    assert config.target("mnt").password == "pan-secret"


def test_target_is_unconfigured_until_it_has_host_and_credentials():
    without_password = _config(environ={})
    assert not without_password.target("pan").configured
    assert "ISE_PASS" in without_password.target("pan").unconfigured_reason()

    configured = _config()
    assert configured.target("pan").configured
    assert configured.target("pan").unconfigured_reason() == ""
    # pxGrid is absent from the document entirely.
    assert not configured.target("pxgrid").configured
    assert "host" in configured.target("pxgrid").unconfigured_reason()


def test_pxgrid_accepts_either_a_password_or_a_client_certificate():
    base = {"host": "px1.example.com", "node_name": "ise-exporter"}
    with_password = _config(
        _document(targets={**_document()["targets"], "pxgrid": base}),
        environ={"ISE_PXGRID_PASSWORD": "px"})
    assert with_password.target("pxgrid").configured

    with_cert = _config(_document(targets={
        **_document()["targets"],
        "pxgrid": {**base, "client_cert": "/c.pem", "client_key": "/c.key"}}),
        environ={})
    assert with_cert.target("pxgrid").configured


@pytest.mark.parametrize("document,message", [
    ({"nonsense": 1}, "top-level"),
    ({"targets": {"switch": {}}}, "targets"),
    ({"targets": {"pan": {"hostname": "x"}}}, "targets.pan"),
    ({"budget": {"pan": {"nope": 1}}}, "budget.pan"),
    ({"datasets": {"not_a_dataset": {}}}, "datasets"),
    ({"datasets": {"deployment": {"cadence": "5m"}}}, "datasets.deployment"),
    ({"exporter": {"listen": 1}}, "exporter"),
])
def test_unknown_keys_are_rejected_and_name_their_section(document, message):
    with pytest.raises(ConfigError, match=message):
        _config(_document(**document))


def test_unknown_profile_lists_the_valid_ones():
    with pytest.raises(ConfigError, match="production"):
        _config(_document(profile="enormous"))


def test_provider_choice_is_validated_against_the_dataset_declaration():
    with pytest.raises(ConfigError, match="choose from"):
        _config(_document(datasets={"active_sessions": {"providers": ["telnet"]}}))
    with pytest.raises(ConfigError, match="twice"):
        _config(_document(datasets={
            "active_sessions": {"providers": ["mnt", "mnt"]}}))
    with pytest.raises(ConfigError, match="non-empty"):
        _config(_document(datasets={"active_sessions": {"providers": []}}))


def test_duty_cycle_is_meaningful_only_for_the_database_target():
    with pytest.raises(ConfigError, match="only the oracle target"):
        _config(_document(budget={"pan": {"duty_cycle_percent": 1.0}}))


def test_a_target_with_no_declared_ceiling_warns_rather_than_passing_silently():
    config = _config(_document(budget={"pxgrid": {"requests_per_hour": 0}}))
    assert any("pxgrid" in warning for warning in config.warnings)


def test_dataset_defaults_apply_when_a_dataset_is_not_mentioned():
    config = _config()
    entry = config.dataset("deployment")
    assert entry.enabled
    assert entry.providers == ()      # means "use the dataset's own order"
    assert entry.interval == 0        # means "use the dataset's default cadence"
