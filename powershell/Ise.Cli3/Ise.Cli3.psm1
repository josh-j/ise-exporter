# ise-cli3: the operator surface for ise-exporter3.
#
# v2's PowerShell module was a 1356-line wrapper that spawned a Python CLI for
# every command. This talks to the exporter's local API instead: no process per
# call, no second implementation of the ISE clients, and no way for an operator
# session to bypass the pacing gate or the authentication guard, because there is
# no second process to bypass them from.
#
# Everything here is read-only.

$script:IseApiRoot = if ($env:ISE_EXPORTER_API) { $env:ISE_EXPORTER_API }
                     else { 'http://127.0.0.1:9619' }

function Get-IseApiRoot {
    <#
    .SYNOPSIS
    Show the exporter API this session is talking to.
    #>
    [CmdletBinding()]
    param()
    [pscustomobject]@{ ApiRoot = $script:IseApiRoot }
}

function Set-IseApiRoot {
    <#
    .SYNOPSIS
    Point this session at a different exporter.
    .EXAMPLE
    Set-IseApiRoot -Uri http://127.0.0.1:9629
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][uri]$Uri)
    if ($PSCmdlet.ShouldProcess($Uri, 'Set exporter API root')) {
        $script:IseApiRoot = $Uri.AbsoluteUri.TrimEnd('/')
    }
    Get-IseApiRoot
}

function Invoke-IseApi {
    <#
    .SYNOPSIS
    Call a read-only exporter API route and return objects.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)

    $uri = "$($script:IseApiRoot)$Path"
    try {
        Invoke-RestMethod -Method Get -Uri $uri -TimeoutSec 10
    }
    catch [System.Net.Http.HttpRequestException], [System.Net.WebException] {
        throw [System.InvalidOperationException]::new(
            "Could not reach the exporter API at $uri. Is ise-exporter3 running? " +
            "Set another address with Set-IseApiRoot.", $_.Exception)
    }
}

function Get-IseHealth {
    <#
    .SYNOPSIS
    One-line answer to "is this exporter healthy and within budget?"
    #>
    [CmdletBinding()]
    param()
    Invoke-IseApi -Path '/api/v1/health'
}

function Get-IseDataset {
    <#
    .SYNOPSIS
    Per-dataset state: which source is live, how stale it is, and why it failed.
    .PARAMETER Name
    Filter by dataset name; wildcards accepted.
    .PARAMETER Unhealthy
    Show only datasets that are failing, degraded, or not scheduled.
    .EXAMPLE
    Get-IseDataset -Unhealthy
    .EXAMPLE
    Get-IseDataset radius* | Format-Table dataset, provider, interval
    #>
    [CmdletBinding()]
    param(
        [Parameter(Position = 0)][string]$Name,
        [switch]$Unhealthy
    )
    $rows = Invoke-IseApi -Path '/api/v1/datasets'
    if ($Name) { $rows = $rows | Where-Object { $_.dataset -like $Name } }
    if ($Unhealthy) {
        $rows = $rows | Where-Object {
            $_.enabled -and ($_.consecutive_failures -gt 0 -or $_.degraded -or -not $_.scheduled)
        }
    }
    $rows
}

function Get-IseProvider {
    <#
    .SYNOPSIS
    Every source declared for each dataset, and which one is live.
    .DESCRIPTION
    Sources differ in meaning, so `supplies` shows what each one can actually
    populate. A dataset running on a fallback often answers a narrower question
    than its preferred source would.
    .PARAMETER Dataset
    Filter by dataset name; wildcards accepted.
    .PARAMETER Active
    Show only the source currently supplying each dataset.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Position = 0)][string]$Dataset,
        [switch]$Active
    )
    $rows = Invoke-IseApi -Path '/api/v1/providers'
    if ($Dataset) { $rows = $rows | Where-Object { $_.dataset -like $Dataset } }
    if ($Active) { $rows = $rows | Where-Object { $_.active } }
    $rows
}

function Get-IseTarget {
    <#
    .SYNOPSIS
    Planned load against the declared budget, per ISE persona.
    #>
    [CmdletBinding()]
    param()
    Invoke-IseApi -Path '/api/v1/targets'
}

function Get-IsePlan {
    <#
    .SYNOPSIS
    The resolved plan: which source supplies each dataset and what it costs.
    .PARAMETER AsText
    Return the same report the `ise-exporter3 plan` command prints.
    #>
    [CmdletBinding()]
    param([switch]$AsText)
    if ($AsText) {
        Invoke-IseApi -Path '/api/v1/plan.txt'
        return
    }
    Invoke-IseApi -Path '/api/v1/plan'
}

function Get-IseDegraded {
    <#
    .SYNOPSIS
    Datasets not running on their preferred source, and what they fell back to.
    .DESCRIPTION
    This is the question v1 could not answer at all: it swapped sources silently,
    so a panel changed meaning with nothing to show for it.
    #>
    [CmdletBinding()]
    param()
    Get-IseDataset | Where-Object { $_.degraded } | ForEach-Object {
        $active = Get-IseProvider -Dataset $_.dataset -Active
        [pscustomobject]@{
            Dataset  = $_.dataset
            Using    = $_.provider
            Target   = $_.target
            Supplies = ($active.supplies -join ', ')
            Notes    = $active.notes
        }
    }
}

Export-ModuleMember -Function @(
    'Get-IseApiRoot', 'Set-IseApiRoot', 'Invoke-IseApi',
    'Get-IseHealth', 'Get-IseDataset', 'Get-IseProvider',
    'Get-IseTarget', 'Get-IsePlan', 'Get-IseDegraded'
)
