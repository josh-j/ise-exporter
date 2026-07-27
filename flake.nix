{
  description = "ise-exporter3 dev shell";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  outputs = { self, nixpkgs }:
    let
      forAll = f: nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ]
        (s: f nixpkgs.legacyPackages.${s});

      # Grafana's Foundation SDK, which generates dashboards3/*.json. Not in
      # nixpkgs, so it is packaged here from the PyPI sdist rather than left to
      # `pip install` -- the dashboards are a committed build artifact and CI
      # diffs them against a fresh render, so the generator has to be pinned or
      # that check compares against whatever pip resolved that morning.
      #
      # Two things about this package are awkward and neither is our doing:
      #
      # - it versions with a PEP 440 *epoch* (`1769699998!10.1.0`), and `!` is
      #   not a legal character in a nix store path name. The fetched file is
      #   therefore given an explicit sanitised `name`, and `sourceRoot` names
      #   the directory inside the tarball, which keeps its epoch and is only
      #   ever a build directory;
      # - the epoch means ordinary version comparison is misleading: every
      #   epoch-carrying release sorts above every plain one, so "10.1.0" here
      #   is the upstream Grafana version this SDK targets, not a sequence
      #   number. pyproject.toml asks for `>=10.1`, which this satisfies.
      foundationSdk = pkgs: pkgs.python312Packages.buildPythonPackage {
        pname = "grafana-foundation-sdk";
        version = "10.1.0";
        pyproject = true;
        src = pkgs.fetchurl {
          name = "grafana-foundation-sdk-10.1.0.tar.gz";
          url = "https://files.pythonhosted.org/packages/source/g/"
            + "grafana-foundation-sdk/"
            + "grafana_foundation_sdk-1769699998%2110.1.0.tar.gz";
          hash = "sha256-js+33Itm/Fy9v36kjcGRaa7QPmmLpJDxABvFAaGUxcc=";
        };
        sourceRoot = "grafana_foundation_sdk-1769699998!10.1.0";
        build-system = [ pkgs.python312Packages.hatchling ];
        # Pure data classes and builders; upstream ships no test suite in the
        # sdist. The import check is the real gate, and the dashboard generator
        # exercises it for real on every CI run.
        doCheck = false;
        pythonImportsCheck = [ "grafana_foundation_sdk" ];
        meta = {
          description =
            "Types and builders for constructing Grafana objects";
          homepage = "https://github.com/grafana/grafana-foundation-sdk";
          license = pkgs.lib.licenses.asl20;
        };
      };
    in {
      packages = forAll (pkgs: {
        grafana-foundation-sdk = foundationSdk pkgs;
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python312.withPackages (p: [
              p.prometheus-client p.requests p.websocket-client p.oracledb
              p.pytest p.ruff p.pip p.build p.hatchling
              (foundationSdk pkgs)
            ]))
          ];
        };
      });
    };
}
