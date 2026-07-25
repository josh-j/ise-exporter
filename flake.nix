{
  description = "ise-exporter3 dev shell";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  outputs = { self, nixpkgs }:
    let
      forAll = f: nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ]
        (s: f nixpkgs.legacyPackages.${s});
    in {
      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python312.withPackages (p: [
              p.prometheus-client p.requests p.oracledb
              p.pytest p.ruff p.pip p.build p.hatchling
            ]))
          ];
        };
      });
    };
}
