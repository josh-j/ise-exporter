# The generated dashboard set as a directory a Grafana file provider can read.
#
# Kept separate from the exporter package on purpose: Grafana and the exporter
# are rarely the same machine, and a dashboard change should not rebuild -- or
# restart -- a collector.
{ lib, runCommand, jq }:

runCommand "ise-exporter3-dashboards"
  {
    src = ../dashboards3;
    nativeBuildInputs = [ jq ];
    meta = {
      description = "Generated Grafana dashboards for ise-exporter3";
      platforms = lib.platforms.all;
    };
  }
  ''
    mkdir -p "$out"
    cp "$src"/*.json "$out"/

    # A dashboard whose uid does not match its filename cannot be reasoned
    # about from a provisioning directory listing, and a malformed one is
    # accepted silently by the file provider and then never appears. Both are
    # cheap to catch here and expensive to notice in Grafana.
    for dashboard in "$out"/*.json; do
        name="$(basename "$dashboard" .json)"
        uid="$(jq -r '.uid' "$dashboard")"
        if [ "$uid" != "$name" ]; then
            echo "$(basename "$dashboard"): uid is '$uid', expected '$name'" >&2
            exit 1
        fi
    done
  ''
