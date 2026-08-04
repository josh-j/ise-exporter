# NixOS module for ise-exporter3.
#
# The declarative equivalent of deploy/install.sh. Everything that script does
# imperatively -- the user, the unit, the hardening, the state directory, the
# CLI on PATH -- is an option here, with one deliberate difference: secrets are
# never written by this module. The credentials file stays root-owned outside
# the store, exactly as the shipped unit expects, because a Nix store path is
# world-readable and a RADIUS shared secret is not.
{ self }:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.ise-exporter3;
  format = pkgs.formats.toml { };

  # settings and configFile are mutually exclusive; settings is the Nix-native
  # path and configFile is the escape hatch for a config managed outside Nix
  # (agenix, an operator-edited file, a config the CLI rewrites).
  generatedConfig = format.generate "ise-exporter3-config.toml" cfg.settings;
  configPath =
    if cfg.configFile != null then cfg.configFile else generatedConfig;
in
{
  options.services.ise-exporter3 = {
    enable = lib.mkEnableOption "the Cisco ISE Prometheus exporter";

    package = lib.mkPackageOption pkgs "ise-exporter3" {
      default = null;
    } // {
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.ise-exporter3;
      defaultText = lib.literalMD "the exporter from this flake";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "ise-exporter3";
      description = "System user the exporter runs as.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "ise-exporter3";
      description = "System group the exporter runs as.";
    };

    settings = lib.mkOption {
      type = format.type;
      default = { };
      example = lib.literalExpression ''
        {
          ise.host = "ise-pan.example.net";
          exporter = { listen = "0.0.0.0"; port = 9645; };
          scale.sessions = 40000;
        }
      '';
      description = ''
        Exporter configuration, rendered to TOML. See
        `ise-exporter3.toml.example` for the full surface.

        This lands in the Nix store and is world-readable, so it must not carry
        passwords. Those belong in {option}`credentialsFile`, which systemd
        reads as root before privileges are dropped.
      '';
    };

    configFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/etc/ise-exporter3/config.toml";
      description = ''
        Path to an existing config file, used instead of {option}`settings`.
        For a configuration managed outside Nix.
      '';
    };

    credentialsFile = lib.mkOption {
      type = lib.types.path;
      default = "/etc/ise-exporter3/credentials";
      description = ''
        Root-owned environment file holding every password. Deliberately not
        managed by this module: put it there with agenix, sops-nix, or
        `ise-exporter3-set-passwords`, mode 0400, owned by root.

        The unit will refuse to start without it, which is the intended
        failure -- an exporter that starts with no credentials collects
        nothing and says so only in its own metrics.
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 9645;
      description = ''
        Port the scrape listener binds. Informational unless
        {option}`openFirewall` is set: the listener itself is configured
        through {option}`settings`, which is the single source of truth.
      '';
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open {option}`port` in the firewall.";
    };

    installCli = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Put `ise-exporter3` on the system PATH, for the read-only operator
        subcommands.
      '';
    };

    grafana = {
      provision = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = ''
          Provision the generated dashboards into a Grafana running on this
          host. Set this on the Grafana machine, which is usually not the one
          running the exporter -- the option lives here so the dashboards and
          the metrics they query cannot drift apart across two repositories.
        '';
      };

      folder = lib.mkOption {
        type = lib.types.str;
        default = "ISE";
        description = "Grafana folder the dashboards are provisioned into.";
      };

      package = lib.mkOption {
        type = lib.types.package;
        default =
          self.packages.${pkgs.stdenv.hostPlatform.system}.ise-exporter3-dashboards;
        defaultText = lib.literalMD "the dashboards from this flake";
        description = "Directory of dashboard JSON to provision.";
      };
    };
  };

  config = lib.mkMerge [
    (lib.mkIf cfg.enable {
      assertions = [
        {
          assertion = cfg.configFile == null || cfg.settings == { };
          message =
            "services.ise-exporter3: set either settings or configFile, not both.";
        }
        {
          assertion = cfg.configFile != null || cfg.settings != { };
          message =
            "services.ise-exporter3: no configuration given; set settings or configFile.";
        }
      ];

      users.users = lib.mkIf (cfg.user == "ise-exporter3") {
        ise-exporter3 = {
          isSystemUser = true;
          group = cfg.group;
          description = "Cisco ISE Prometheus exporter";
        };
      };
      users.groups = lib.mkIf (cfg.group == "ise-exporter3") {
        ise-exporter3 = { };
      };

      environment.systemPackages = lib.mkIf cfg.installCli [ cfg.package ];

      networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];

      systemd.services.ise-exporter3 = {
        description = "Cisco ISE Prometheus Exporter 3";
        wantedBy = [ "multi-user.target" ];
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];

        # Rate-limited restarts: a deployment whose credentials or schema are
        # wrong should stop and stay stopped rather than hammer the appliance.
        startLimitIntervalSec = 3600;
        startLimitBurst = 3;

        environment = {
          ISE_EXPORTER3_CONFIG = configPath;
          PYTHONUNBUFFERED = "1";
        };

        serviceConfig = {
          Type = "simple";
          User = cfg.user;
          Group = cfg.group;
          EnvironmentFile = cfg.credentialsFile;
          # `plan` validates the configuration and the declared cost before a
          # single request leaves the process. Failing here is the cheap
          # failure; failing after connecting is not.
          ExecStartPre =
            "${lib.getExe cfg.package} plan --config ${configPath}";
          ExecStart = "${lib.getExe cfg.package} run --config ${configPath}";
          Restart = "on-failure";
          RestartSec = "5min";

          # The production-scale simulator peaks around 480 MiB RSS. Leave room
          # for Python/TLS overhead while containing malformed responses and
          # scrape storms.
          MemoryHigh = "768M";
          MemoryMax = "1G";
          TasksMax = 64;
          LimitNOFILE = 1024;
          OOMScoreAdjust = 500;

          StateDirectory = "ise-exporter3";
          StateDirectoryMode = "0750";
          UMask = "0007";

          NoNewPrivileges = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          PrivateTmp = true;
          PrivateDevices = true;
          ProtectKernelTunables = true;
          ProtectKernelModules = true;
          ProtectControlGroups = true;
          ProtectClock = true;
          RestrictSUIDSGID = true;
          RestrictRealtime = true;
          LockPersonality = true;
          AmbientCapabilities = "";
          # Outbound HTTPS to ISE and an inbound scrape; nothing else.
          RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
          SystemCallFilter = [ "@system-service" "~@privileged" "~@resources" ];
          SystemCallArchitectures = "native";
        };
      };
    })

    (lib.mkIf cfg.grafana.provision {
      services.grafana.provision.dashboards.settings.providers = [
        {
          name = "ise-exporter3";
          type = "file";
          folder = cfg.grafana.folder;
          # The dashboards are generated and contract-tested; an edit made in
          # the Grafana UI would be silently reverted on the next deploy, so
          # say so rather than let someone lose work.
          allowUiUpdates = false;
          options.path = cfg.grafana.package;
        }
      ];
    })
  ];
}
