# Interactive profile for the ise-exporter3 operator REPL.
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Ise.Cli3/Ise.Cli3.psd1') -Force

function prompt { "ise> " }

# Default table views, so the common commands read well without piping.
Update-FormatData -PrependPath (Join-Path $PSScriptRoot 'Ise.Cli3/Ise.Cli3.format.ps1xml')

Write-Host ''
Write-Host '  ise-exporter3 operator shell' -ForegroundColor Cyan
Write-Host "  api: $((Get-IseApiRoot).ApiRoot)" -ForegroundColor DarkGray
Write-Host ''
Write-Host '  Get-IseHealth              is it healthy and inside its budget'
Write-Host '  Get-IseDataset -Unhealthy  what is failing, degraded, or unscheduled'
Write-Host '  Get-IseDegraded            what fell back, and to what'
Write-Host '  Get-IseProvider -Active    which source is supplying each dataset'
Write-Host '  Get-IseTarget              planned load against the declared budget'
Write-Host '  Get-IsePlan -AsText        the full plan report'
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
