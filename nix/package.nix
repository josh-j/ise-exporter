# The exporter as a Python application.
#
# Runtime dependencies only. The Grafana Foundation SDK is deliberately absent:
# dashboards are a committed build artifact, so rendering them is a developer
# concern (the dev shell) and never a deployment one.
{ lib
, python3Packages
, version ? "3.0.0.dev0"
}:

python3Packages.buildPythonApplication {
  pname = "ise-exporter3";
  inherit version;
  pyproject = true;

  src = lib.cleanSourceWith {
    src = ../.;
    # Keep the store path stable across dashboard regeneration and local venvs:
    # neither changes what the application does, and both would otherwise cause
    # a rebuild of the service on every dashboard edit.
    filter = path: type:
      let base = baseNameOf (toString path);
      in !(lib.hasPrefix "." base && base != ".")
         && base != "dashboards3"
         && base != "dist"
         && base != "__pycache__";
  };

  build-system = [ python3Packages.hatchling ];

  dependencies = with python3Packages; [
    prometheus-client
    requests
    websocket-client
    oracledb
  ];

  # The suite is hermetic -- it drives the real scheduler, transports and
  # datasets against a synthetic ISE on a virtual clock, and never touches the
  # network. Running it here is the point: a package that builds but cannot
  # collect is not a working deployment.
  nativeCheckInputs = with python3Packages; [ pytestCheckHook ];
  pytestFlags = [ "tests" ];
  disabledTests = [
    # Needs the Grafana SDK, which is a build-time-only dependency and is not
    # in the runtime closure. CI renders and diffs the dashboards instead.
    "test_the_committed_dashboards_match_a_fresh_build"
  ];
  # The dashboard contract tests read dashboards3/, which the source filter
  # above excludes from the build. They run in CI and the dev shell.
  disabledTestPaths = [ "tests/test_v3_dashboards.py" ];

  pythonImportsCheck = [ "ise_exporter3" ];

  meta = {
    description =
      "Prometheus exporter for Cisco ISE with provider adapters and declared load";
    homepage = "https://github.com/josh-j/ise-exporter";
    mainProgram = "ise-exporter3";
    platforms = lib.platforms.linux;
  };
}
