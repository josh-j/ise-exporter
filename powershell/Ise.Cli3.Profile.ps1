# Interactive profile for the ise-exporter3 operator REPL.
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Ise.Cli3/Ise.Cli3.psd1') -Force

function prompt { "ise> " }

Write-Host ''
Write-Host '  ise-exporter3 operator shell' -ForegroundColor Cyan
Write-Host "  api: $((Get-IseApiRoot).ApiRoot)" -ForegroundColor DarkGray
Write-Host ''
# Two groups, because they cost different things. The split is the point: the
# first answers from state the exporter already computed, the second spends the
# same Oracle budget a scheduled collection does.
Write-Host '  local state (free)' -ForegroundColor Cyan
Write-Host '  Get-IseHealth              is it healthy and inside its budget'
Write-Host '  Get-IseDataset -Unhealthy  what is failing, degraded, or unscheduled'
Write-Host '  Get-IseDegraded            what fell back, and to what'
Write-Host '  Get-IseProvider -Active    which source is supplying each dataset'
Write-Host '  Get-IseTarget              planned load against the declared budget'
Write-Host '  Get-IsePlan -AsText        the full plan report'
Write-Host ''
Write-Host '  Data Connect (paced Oracle reads)' -ForegroundColor Cyan
Write-Host '  Get-IseDcStatus            configured, discovered, busy, cooling down'
Write-Host '  Get-IseDcView              the reporting views, with their columns'
Write-Host '  Get-IseDcRadiusAuth -Failed -Last 2h        who failed, and why'
Write-Host '  Get-IseRadiusLiveLog -Last 1h               the Live Logs screen'
Write-Host '  Get-IseContextVisibility -Last 1h           the Context Visibility screen'
Write-Host '  Invoke-IseDcQuery -View <name> -Last 1h     anything else'
Write-Host '  ...every Dc cmdlet charges the declared duty cycle; -AsSql is free' `
    -ForegroundColor DarkGray
Write-Host '  ...the default table is curated; -All shows every column returned' `
    -ForegroundColor DarkGray
Write-Host ''

try {
    $health = Get-IseHealth
    $state = if ($health.fits_budget) { 'within budget' } else { 'OVER BUDGET' }
    Write-Host ("  {0} datasets enabled, {1} collecting, {2}" -f `
        $health.datasets_enabled, $health.datasets_collecting, $state) -ForegroundColor DarkGray
    if ($health.datasets_degraded) {
        Write-Host ("  degraded: {0}" -f ($health.datasets_degraded -join ', ')) `
            -ForegroundColor Yellow
    }
    Write-Host ''
}
catch {
    Write-Host "  exporter not reachable: $($_.Exception.Message)" -ForegroundColor Yellow
    Write-Host ''
}
