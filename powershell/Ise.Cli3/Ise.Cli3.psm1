# ise-cli3: the operator surface for ise-exporter3.
#
# v2's PowerShell module was a 1356-line wrapper that spawned a Python CLI for
# every command. This talks to the exporter's local API instead: no process per
# call, no second implementation of the ISE clients, and no way for an operator
# session to bypass the pacing gate or the authentication guard, because there is
# no second process to bypass them from.
#
# Everything here is read-only. The Get-IseDc* family is the only part that
# reaches ISE at all, and it reaches it through the exporter's own Data Connect
# transport -- same pacing gate, same adaptive cooldown, same row and byte
# ceilings -- so an ad-hoc query is charged against the declared duty cycle
# exactly like a scheduled collection. That is what makes the `Dc` in the noun a
# cost mark: everything without it answers from state the exporter already
# computed, and is free.

$script:IseApiRoot = if ($env:ISE_EXPORTER_API) { $env:ISE_EXPORTER_API }
                     else { 'http://127.0.0.1:9619' }

# View descriptors never touch Oracle, but tab completion reads them on every
# keystroke, so they are fetched once per session. -Refresh is the escape hatch
# for a catalog that finished discovering after the shell started.
$script:IseDcViews = $null

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
        # A different exporter has a different catalog, and a stale one would
        # complete view names that do not exist there.
        $script:IseDcViews = $null
    }
    Get-IseApiRoot
}

function ConvertTo-IseQueryString {
    <#
    .SYNOPSIS
    Encode a parameter hashtable, repeating a parameter once per array element.
    #>
    param([hashtable]$Query)

    # Keys are emitted in sorted order so the same command always produces the
    # same URL: a hashtable has no order, and an unstable query string is
    # impossible to diff against the server's own tests.
    $parts = foreach ($key in ($Query.Keys | Sort-Object)) {
        foreach ($value in @($Query[$key])) {
            if ($null -eq $value -or $value -eq '') { continue }
            '{0}={1}' -f [uri]::EscapeDataString([string]$key),
                          [uri]::EscapeDataString([string]$value)
        }
    }
    $parts -join '&'
}

function New-IseApiError {
    <#
    .SYNOPSIS
    Turn a refusal from the exporter into a sentence an operator can act on.
    #>
    param([int]$Status, $Body, [string]$Uri)

    # A guard saying no is information, not a stack trace. The body names which
    # guard and, for a cooldown, how long it wants; both belong in the message.
    $detail = $null
    if ($Body -is [string]) {
        $text = $Body
        $Body = $null
        if ($text) { try { $Body = $text | ConvertFrom-Json } catch { $Body = $null } }
        # Routes outside the JSON namespace answer in text; that text is still
        # the only thing the exporter said, so it becomes the detail.
        if ($null -eq $Body) { $detail = $text.Trim() }
    }
    $code = $null
    $retry = $null
    if ($Body -is [psobject]) {
        $names = @($Body.PSObject.Properties.Name)
        if ($names -contains 'error') { $code = [string]$Body.error }
        if ($names -contains 'detail') { $detail = [string]$Body.detail }
        if ($names -contains 'retry_after_seconds' -and $null -ne $Body.retry_after_seconds) {
            $retry = [double]$Body.retry_after_seconds
        }
    }

    $message = switch ($code) {
        'cooldown' {
            $seconds = if ($null -ne $retry) { [math]::Ceiling($retry) } else { 0 }
            if ($seconds -gt 0) {
                "Data Connect is cooling down; retry in ${seconds}s. Pass -Wait to sit it out."
            } else {
                'Data Connect is cooling down. Pass -Wait to sit it out.'
            }
        }
        'busy' {
            'Another Data Connect query is already running; the exporter runs ' +
            'one at a time. Pass -Wait to queue behind it.'
        }
        'schema_pending' {
            'The exporter has not finished discovering the Data Connect ' +
            'catalog yet, so it cannot validate a view or a column. ' +
            'Get-IseDcStatus shows when it has.'
        }
        'dataconnect_unconfigured' {
            'This exporter has no Data Connect target configured, so there is ' +
            'nothing for the Get-IseDc* cmdlets to read.'
        }
        'unknown_view' { "That is not a legal view name. Get-IseDcView lists what this account can see." }
        'view_unavailable' {
            'The Data Connect account cannot see that view; Get-IseDcView ' +
            'reports which ones it can.'
        }
        'invalid_request' { 'The exporter refused the request as malformed.' }
        default {
            # An unrecognised refusal keeps its status code: this is the branch a
            # contract drift lands in, and the number is the first clue.
            if ($code) { "The exporter refused the request (HTTP $Status): $code." }
            else { "The exporter answered HTTP $Status." }
        }
    }
    if ($detail) { $message = "$message ($detail)" }

    $information = [pscustomobject]@{
        Status            = $Status
        error             = $code
        detail            = $detail
        retry_after_seconds = $retry
        Uri               = $Uri
    }
    [System.Management.Automation.ErrorRecord]::new(
        [System.InvalidOperationException]::new($message),
        $(if ($code) { $code } else { "http_$Status" }),
        [System.Management.Automation.ErrorCategory]::InvalidOperation,
        $information)
}

function Invoke-IseApi {
    <#
    .SYNOPSIS
    Call a read-only exporter API route and return objects.
    .DESCRIPTION
    HTTP failures are not exceptional here: every one of them carries a JSON
    body saying which guard refused and why, so they are checked rather than
    thrown, and turned into a readable message. The parsed body rides on the
    error record's TargetObject, which is how Invoke-IseDcQuery -Wait knows a
    cooldown from a malformed request.
    .PARAMETER Path
    Route path, beginning with a slash.
    .PARAMETER Query
    Query-string parameters. An array value is emitted as a repeated parameter,
    which is how the Data Connect routes take more than one filter.
    .PARAMETER TimeoutSec
    Defaults to 10, which suits routes that answer from computed state. Data
    Connect queries pass 90: they wait on the shared pacing gate before Oracle
    has even started.
    .EXAMPLE
    Invoke-IseApi -Path /api/v1/dataconnect/views
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Path,
        [hashtable]$Query,
        [int]$TimeoutSec = 10
    )

    $uri = "$($script:IseApiRoot)$Path"
    if ($Query -and $Query.Count) {
        $encoded = ConvertTo-IseQueryString -Query $Query
        if ($encoded) { $uri = "${uri}?${encoded}" }
    }

    $status = 0
    try {
        # -SkipHttpErrorCheck leaves the catch below meaning exactly one thing:
        # the exporter was not reachable at all.
        $response = Invoke-RestMethod -Method Get -Uri $uri -TimeoutSec $TimeoutSec `
            -SkipHttpErrorCheck -StatusCodeVariable status
    }
    catch [System.Net.Http.HttpRequestException], [System.Net.WebException],
          [System.OperationCanceledException] {
        throw [System.InvalidOperationException]::new(
            "Could not reach the exporter API at $uri. Is ise-exporter3 running? " +
            "Set another address with Set-IseApiRoot.", $_.Exception)
    }

    if ($status -ge 400) {
        $PSCmdlet.ThrowTerminatingError(
            (New-IseApiError -Status $status -Body $response -Uri $uri))
    }
    $response
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

# --- Data Connect: everything below reaches Oracle through the paced transport.

function ConvertTo-IseDcTypeName {
    <#
    .SYNOPSIS
    The PSTypeName rows of one view carry, so formats and filters can name them.
    #>
    param([string]$View)
    $words = @($View -split '[^A-Za-z0-9]+' | Where-Object { $_ })
    if (-not $words) { return 'Ise.Dc.Row' }
    'Ise.Dc.' + (($words | ForEach-Object {
        $_.Substring(0, 1).ToUpperInvariant() + $_.Substring(1).ToLowerInvariant()
    }) -join '')
}

function Get-IseDcViewCache {
    <#
    .SYNOPSIS
    The session's view descriptors, fetched at most once.
    #>
    param([switch]$Refresh)
    if ($Refresh -or $null -eq $script:IseDcViews) {
        $descriptors = @(Invoke-IseApi -Path '/api/v1/dataconnect/views')
        foreach ($descriptor in $descriptors) {
            $descriptor.PSObject.TypeNames.Insert(0, 'Ise.Dc.View')
            # The row type is derived here rather than documented elsewhere, so
            # "what type do rows of this view have" is answerable from the object
            # an operator already has in front of them.
            Add-Member -InputObject $descriptor -NotePropertyName 'TypeName' `
                -NotePropertyValue (ConvertTo-IseDcTypeName -View $descriptor.name) -Force
        }
        $script:IseDcViews = $descriptors
    }
    $script:IseDcViews
}

function Get-IseDcView {
    <#
    .SYNOPSIS
    The Data Connect reporting views this exporter curates, and their columns.
    .DESCRIPTION
    Free: the descriptors come from the catalog the exporter discovered on its
    own schedule, not from a fresh query. `available` false means the Data
    Connect account cannot see that view, so nothing will ever read it.

    A view with a null time_column is current state rather than history, and
    rejects -Last.
    .PARAMETER Name
    Filter by view name; wildcards accepted. Every view the Data Connect
    account can see is listed -- curated ones carry windows, default order
    and typed cmdlets; the rest are queryable exactly as the catalog shows
    them (the curated property tells them apart).
    .PARAMETER Refresh
    Re-fetch the descriptors. Worth doing once if the shell started before the
    exporter finished discovering the catalog.
    .EXAMPLE
    Get-IseDcView
    .EXAMPLE
    Get-IseDcView tacacs* | Format-List name, time_column, default_columns
    #>
    [CmdletBinding()]
    param(
        [Parameter(Position = 0)][string]$Name,
        [switch]$Refresh
    )
    $views = Get-IseDcViewCache -Refresh:$Refresh
    if ($Name) { $views = $views | Where-Object { $_.name -like $Name -or $_.view -like $Name } }
    $views
}

function Get-IseDcColumn {
    <#
    .SYNOPSIS
    Columns one view actually carries, and which ones it returns by default.
    .DESCRIPTION
    The column list is the discovered catalog, so it is what the exporter will
    validate -Filter, -Match, -Column and -OrderBy against. Anything not listed
    here is refused before a statement is built.
    .PARAMETER View
    Curated view name.
    .PARAMETER Name
    Filter by column name; wildcards accepted.
    .PARAMETER Refresh
    Re-fetch the descriptors first.
    .EXAMPLE
    Get-IseDcColumn -View radius_authentications
    .EXAMPLE
    Get-IseDcColumn -View endpoints_data *mac*
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory, Position = 0)][string]$View,
        [Parameter(Position = 1)][string]$Name,
        [switch]$Refresh
    )
    $descriptor = @(Get-IseDcView -Name $View -Refresh:$Refresh) | Select-Object -First 1
    if (-not $descriptor) {
        throw [System.InvalidOperationException]::new(
            "No view matches '$View'. Get-IseDcView lists what this account can see.")
    }
    $defaults = @($descriptor.default_columns)
    foreach ($column in @($descriptor.columns)) {
        if ($Name -and $column -notlike $Name) { continue }
        [pscustomobject]@{
            PSTypeName = 'Ise.Dc.Column'
            View       = $descriptor.name
            Column     = $column
            Default    = ($defaults -contains $column)
        }
    }
}

function Get-IseDcStatus {
    <#
    .SYNOPSIS
    Whether Data Connect is configured, discovered, busy, or cooling down.
    .DESCRIPTION
    Cheap: it answers from the exporter's own state and never touches Oracle.
    This is the cmdlet to run when a Get-IseDc* call was refused, because it
    says which of the guards is the one saying no.
    .EXAMPLE
    Get-IseDcStatus
    #>
    [CmdletBinding()]
    param()
    $status = Invoke-IseApi -Path '/api/v1/dataconnect/status'
    if ($status -is [psobject]) { $status.PSObject.TypeNames.Insert(0, 'Ise.Dc.Status') }
    $status
}

function Invoke-IseDcQuery {
    <#
    .SYNOPSIS
    Run one bounded query against a Data Connect reporting view.
    .DESCRIPTION
    The SQL is built and validated server-side from these parameters: column
    names are checked against the discovered catalog and values only ever travel
    as binds, so nothing typed here can reach Oracle as SQL. The statement runs
    on the exporter's own transport, which means it waits for the shared pacing
    gate and charges the declared duty cycle exactly like a scheduled collection.

    Rows come back as objects typed Ise.Dc.<View>, so the pipeline, the format
    views and Where-Object all work on them normally.
    .PARAMETER View
    Curated view name; completes from the session's descriptor cache.
    .PARAMETER Filter
    Equality filters as @{ COLUMN = 'value' }.
    .PARAMETER Match
    Pattern filters as @{ COLUMN = 'pat*ern' }, using PowerShell wildcards; the
    server translates them and matches case-insensitively.
    .PARAMETER Last
    Time window: 30m, 2h, 1d. Clamped to the exporter's window ceiling, and
    rejected for views that carry no time column. 'all' drops the time bound
    entirely and makes -First the only bound: the newest N rows, however old.
    The statement timeout still applies, and the scan is charged what it
    really cost.
    .PARAMETER Column
    Projection. By default the whole row comes back -- every column the account
    can see -- and the format views trim what a table displays; name columns
    here to fetch less on purpose.
    .PARAMETER OrderBy
    Order column. Naming one also chooses the direction: ascending unless
    -Descending is given, rather than silently keeping the server's time-DESC
    default.
    .PARAMETER Descending
    Order descending.
    .PARAMETER First
    Row limit; server default 100, clamped server-side.
    .PARAMETER AsSql
    Return the SQL and binds the exporter would run, without touching Oracle.
    Free, and allowed even during a cooldown -- so a heavy query can be judged
    before it is spent.
    .PARAMETER Wait
    When the query is refused because another one is in flight or the duty cycle
    is still being paid off, wait the requested time and retry instead of
    failing.
    .PARAMETER Force
    Run now instead of waiting out the duty-cycle cooldown, and charge only the
    measured Oracle time rather than the amplified cooldown. An override of the
    waits, not of the guards: the row and byte ceilings, the statement timeout,
    the authentication guard and the one-at-a-time lane all still apply, and at
    least the hard floor between statements is still charged. For the statement
    that cannot wait, not for making every statement immediate.
    .PARAMETER Min
    Inclusive lower bounds as @{ COLUMN = value }; with -Max on the same
    column it says between. Values travel as binds like every filter.
    .PARAMETER Max
    Inclusive upper bounds as @{ COLUMN = value }.
    .PARAMETER Exclude
    Exclusions as @{ COLUMN = value }: rows where COLUMN equals the value are
    left out.
    .PARAMETER IsNull
    Columns that must be NULL.
    .PARAMETER NotNull
    Columns that must not be NULL.
    .PARAMETER GroupBy
    Group server-side by up to three columns. The result is a bounded top-N:
    group columns plus aggregates (COUNT by default), largest-first, capped by
    -First -- the whole fleet aggregated in one statement instead of paging
    rows through Group-Object.
    .PARAMETER Aggregate
    Aggregates as function:COLUMN (count, sum, avg, min, max), up to five.
    With -GroupBy they project per group; alone they aggregate the whole
    window into one row. Results carry derived names: avg:RESPONSE_TIME comes
    back as avg_response_time.
    .EXAMPLE
    Invoke-IseDcQuery -View radius_authentications -Last 2h -First 20
    .EXAMPLE
    Invoke-IseDcQuery -View radius_errors_view -Last 4h -Match @{ NETWORK_DEVICE_NAME = 'core-*' }
    .EXAMPLE
    Invoke-IseDcQuery -View radius_authentications -Last 1h -Min @{ RESPONSE_TIME = 500 }
    .EXAMPLE
    Invoke-IseDcQuery -View radius_authentications -Last 1d -GroupBy DEVICE_NAME -Aggregate avg:RESPONSE_TIME, max:RESPONSE_TIME
    .EXAMPLE
    Invoke-IseDcQuery -View endpoints_data -Filter @{ ENDPOINT_POLICY = 'Cisco-IP-Phone' } -AsSql
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory, Position = 0)][string]$View,
        [hashtable]$Filter,
        [hashtable]$Match,
        [hashtable]$Min,
        [hashtable]$Max,
        [hashtable]$Exclude,
        [string[]]$IsNull,
        [string[]]$NotNull,
        [string[]]$GroupBy,
        [string[]]$Aggregate,
        [string]$Last,
        [string[]]$Column,
        [string]$OrderBy,
        [switch]$Descending,
        [int]$First,
        [switch]$AsSql,
        [switch]$Wait,
        [switch]$Force
    )

    $query = @{ view = $View }
    if ($Last) { $query['last'] = $Last }
    $pairs = @{ eq = $Filter; like = $Match; ge = $Min; le = $Max; ne = $Exclude }
    foreach ($name in ($pairs.Keys | Sort-Object)) {
        $table = $pairs[$name]
        if ($table -and $table.Count) {
            $query[$name] = @(foreach ($key in ($table.Keys | Sort-Object)) { "${key}:$($table[$key])" })
        }
    }
    if ($IsNull) { $query['null'] = @($IsNull) }
    if ($NotNull) { $query['notnull'] = @($NotNull) }
    # Group order is projection order, so it is the one array not sorted.
    if ($GroupBy) { $query['group'] = @($GroupBy) }
    if ($Aggregate) { $query['agg'] = @($Aggregate) }
    if ($Column) { $query['cols'] = ($Column -join ',') }
    if ($PSBoundParameters.ContainsKey('OrderBy')) {
        $query['order'] = $OrderBy
        $query['desc'] = if ($Descending) { '1' } else { '0' }
    }
    elseif ($Descending) { $query['desc'] = '1' }
    if ($PSBoundParameters.ContainsKey('First')) { $query['first'] = [string]$First }
    if ($AsSql) { $query['explain'] = '1' }
    if ($Force) { $query['force'] = '1' }

    $response = $null
    $waited = 0
    while ($true) {
        try {
            $response = Invoke-IseApi -Path '/api/v1/dataconnect/query' -Query $query -TimeoutSec 90
            break
        }
        catch {
            $refusal = $_.TargetObject
            if (-not $Wait -or $refusal.error -notin @('busy', 'cooldown')) {
                # Rethrown as the record rather than as the exception, so the
                # parsed refusal survives for whoever is catching this.
                $PSCmdlet.ThrowTerminatingError($_)
            }
            # The exporter said how long it wants; honouring that rather than a
            # local guess is what keeps -Wait from becoming a poll loop.
            $delay = if ($refusal.retry_after_seconds) {
                [math]::Ceiling([double]$refusal.retry_after_seconds)
            } else { 5 }
            $delay = [math]::Max(1, [math]::Min(60, $delay))
            for ($second = $delay; $second -gt 0; $second--) {
                Write-Progress -Id 1 -Activity "Waiting for Data Connect ($($refusal.error))" `
                    -Status "waited ${waited}s so far; retrying in ${second}s" -SecondsRemaining $second
                Start-Sleep -Seconds 1
                $waited++
            }
        }
    }
    if ($waited) { Write-Progress -Id 1 -Activity 'Waiting for Data Connect' -Completed }

    if ($AsSql) {
        [pscustomobject]@{
            PSTypeName = 'Ise.Dc.Sql'
            View       = $response.view
            Sql        = $response.sql
            Binds      = $response.binds
        }
        return
    }

    Write-Verbose ("$($response.view): $($response.row_count) rows in " +
                   "$($response.elapsed_seconds)s; cooldown $($response.cooldown_seconds)s")

    # Grouped rows are a different shape from the view's rows, and the view's
    # format table would render them as empty columns; the generic type lets
    # PowerShell show what actually came back.
    $typeName = if ($GroupBy -or $Aggregate) { 'Ise.Dc.Grouped' }
                else { ConvertTo-IseDcTypeName -View $(if ($response.view) { $response.view } else { $View }) }
    foreach ($row in @($response.rows)) {
        if ($null -eq $row) { continue }
        $row.PSObject.TypeNames.Insert(0, $typeName)
        $row
    }

    if ($response.truncated) {
        # A truncated list looks exactly like a complete one, and that silence is
        # how a coverage gap goes unnoticed.
        Write-Warning ("Stopped at $($response.row_count) rows; there may be more. " +
                       'Raise -First (the exporter caps it) or narrow the window.')
    }
}

function Add-IseDcTerm {
    <#
    .SYNOPSIS
    Route one typed-cmdlet parameter to an equality or a pattern filter.
    #>
    param([hashtable]$Filter, [hashtable]$Match, [string]$Column, [string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) { return }
    # The wildcard is the operator's own choice of operator: * or ? means LIKE,
    # anything else is exact. Both travel as binds either way.
    if ($Value -match '[*?]') { $Match[$Column] = $Value } else { $Filter[$Column] = $Value }
}

function Resolve-IseDcColumn {
    <#
    .SYNOPSIS
    Pick the spelling of a filter column that this appliance actually carries.
    #>
    param([string]$View, [string[]]$Candidate)

    # docs/DATASETS_FACTS.md pins most of these spellings from a live catalogue,
    # but a few -- the RADIUS detail view's user and MAC columns among them --
    # are not in the sets it enumerates, and ISE is not consistent across views.
    # Rather than guess and spend a query on an ORA-00904, ask the descriptor the
    # exporter already published; it is the discovered catalog and it is cached.
    # If it cannot be read, the first candidate goes out and the server produces
    # the authoritative refusal.
    $columns = $null
    try { $columns = @(Get-IseDcView -Name $View | Select-Object -First 1).columns } catch { }
    if ($columns) {
        foreach ($name in $Candidate) { if ($columns -contains $name) { return $name } }
    }
    $Candidate[0]
}

function Invoke-IseDcTypedQuery {
    <#
    .SYNOPSIS
    Shared body of the typed cmdlets: one call, a different column mapping each.
    #>
    param([string]$View, [hashtable]$Filter, [hashtable]$Match, [hashtable]$Bound)

    $arguments = @{ View = $View }
    if ($Filter.Count) { $arguments['Filter'] = $Filter }
    if ($Match.Count) { $arguments['Match'] = $Match }
    foreach ($name in @('Last', 'First', 'Column', 'Wait', 'Force')) {
        if ($Bound.ContainsKey($name)) { $arguments[$name] = $Bound[$name] }
    }
    Invoke-IseDcQuery @arguments
}

function Get-IseDcRadiusAuth {
    <#
    .SYNOPSIS
    RADIUS authentication events, one row per attempt.
    .DESCRIPTION
    Pass and fail are carried by the FAILED flag on this view, not by a status
    string: the exporter's own datasets read NVL(failed,0)=0 as passed, so
    -Passed and -Failed filter on that same column.
    .PARAMETER User
    User name; wildcards accepted.
    .PARAMETER Mac
    Endpoint MAC as ISE spells it in CALLING_STATION_ID; wildcards accepted.
    .PARAMETER Nad
    Network device name; wildcards accepted.
    .PARAMETER Failed
    Only failed attempts.
    .PARAMETER Passed
    Only successful attempts.
    .EXAMPLE
    Get-IseDcRadiusAuth -Failed -Last 2h | Group-Object failure_reason
    .EXAMPLE
    Get-IseDcRadiusAuth -Mac 'AA:BB:CC:*' -Last 1d
    #>
    [CmdletBinding()]
    param(
        [string]$User,
        [string]$Mac,
        [string]$Nad,
        [switch]$Failed,
        [switch]$Passed,
        [string]$Last,
        [int]$First,
        [string[]]$Column,
        [switch]$Wait,
        [switch]$Force
    )
    if ($Failed -and $Passed) {
        throw [System.ArgumentException]::new(
            'Pass -Failed or -Passed, not both; together they exclude every row.')
    }
    $view = 'radius_authentications'
    $filter = @{}
    $match = @{}
    if ($User) {
        Add-IseDcTerm $filter $match (Resolve-IseDcColumn $view 'USERNAME', 'USER_NAME') $User
    }
    if ($Mac) {
        Add-IseDcTerm $filter $match (Resolve-IseDcColumn $view 'CALLING_STATION_ID', 'MAC_ADDRESS') $Mac
    }
    if ($Nad) { Add-IseDcTerm $filter $match 'DEVICE_NAME' $Nad }
    if ($Failed) { $filter['FAILED'] = '1' }
    if ($Passed) { $filter['FAILED'] = '0' }
    Invoke-IseDcTypedQuery -View $view -Filter $filter -Match $match -Bound $PSBoundParameters
}

function Get-IseDcRadiusAccounting {
    <#
    .SYNOPSIS
    RADIUS accounting records: session starts, stops, and durations.
    .PARAMETER User
    User name; wildcards accepted.
    .PARAMETER Mac
    Endpoint MAC; wildcards accepted.
    .PARAMETER Nad
    Network device name; wildcards accepted.
    .EXAMPLE
    Get-IseDcRadiusAccounting -Mac 'AA:BB:CC:*' -Last 1d
    #>
    [CmdletBinding()]
    param(
        [string]$User,
        [string]$Mac,
        [string]$Nad,
        [string]$Last,
        [int]$First,
        [string[]]$Column,
        [switch]$Wait,
        [switch]$Force
    )
    $view = 'radius_accounting'
    $filter = @{}
    $match = @{}
    if ($User) {
        Add-IseDcTerm $filter $match (Resolve-IseDcColumn $view 'USERNAME', 'USER_NAME') $User
    }
    if ($Mac) {
        Add-IseDcTerm $filter $match (Resolve-IseDcColumn $view 'CALLING_STATION_ID', 'MAC_ADDRESS') $Mac
    }
    if ($Nad) { Add-IseDcTerm $filter $match 'DEVICE_NAME' $Nad }
    Invoke-IseDcTypedQuery -View $view -Filter $filter -Match $match -Bound $PSBoundParameters
}

function Get-IseDcRadiusError {
    <#
    .SYNOPSIS
    RADIUS errors by message code and network device.
    .DESCRIPTION
    This view spells its device column NETWORK_DEVICE_NAME where the
    authentication views say DEVICE_NAME, so -Nad exists partly to spare the
    operator that inconsistency.
    .PARAMETER Nad
    Network device name; wildcards accepted.
    .EXAMPLE
    Get-IseDcRadiusError -Last 4h -Nad 'core-*'
    #>
    [CmdletBinding()]
    param(
        [string]$Nad,
        [string]$Last,
        [int]$First,
        [string[]]$Column,
        [switch]$Wait,
        [switch]$Force
    )
    $filter = @{}
    $match = @{}
    if ($Nad) { Add-IseDcTerm $filter $match 'NETWORK_DEVICE_NAME' $Nad }
    Invoke-IseDcTypedQuery -View 'radius_errors_view' -Filter $filter -Match $match `
        -Bound $PSBoundParameters
}

function Get-IseDcEndpoint {
    <#
    .SYNOPSIS
    The endpoint database: MAC, profile, and identity group.
    .DESCRIPTION
    Current state rather than history: without -Last it returns the whole
    database (up to -First), and it is the largest view on a real deployment --
    tens of thousands of rows against a few dozen sessions -- so -First matters
    here more than anywhere else. With -Last it narrows to endpoints whose row
    changed inside the window (bounded on UPDATE_TIME, newest first), which is
    the "what changed lately" question rather than a smaller copy of the same
    list.

    Not every column is equally fresh, and that decides what a query is worth:
    Cisco documents a real-time set -- (Get-IseDcView endpoints_data)
    .realtime_columns, profiling and registration state mostly -- while every
    other column synchronizes with a delay of up to 12 hours. Re-polling the
    delayed columns inside that interval spends duty cycle on answers that
    cannot have changed yet, and a short -Last window over them can be
    legitimately empty because the sync simply has not run.
    .PARAMETER Mac
    MAC address; wildcards accepted.
    .PARAMETER Policy
    Endpoint policy (the profile); wildcards accepted.
    .PARAMETER Last
    Only endpoints updated inside this window: 30m, 2h, 1d.
    .EXAMPLE
    Get-IseDcEndpoint -Policy 'Cisco-IP-Phone*' -First 500
    .EXAMPLE
    Get-IseDcEndpoint -Last 1h
    #>
    [CmdletBinding()]
    param(
        [string]$Mac,
        [string]$Policy,
        [string]$Last,
        [int]$First,
        [string[]]$Column,
        [switch]$Wait,
        [switch]$Force
    )
    $filter = @{}
    $match = @{}
    if ($Mac) { Add-IseDcTerm $filter $match 'MAC_ADDRESS' $Mac }
    if ($Policy) { Add-IseDcTerm $filter $match 'ENDPOINT_POLICY' $Policy }
    Invoke-IseDcTypedQuery -View 'endpoints_data' -Filter $filter -Match $match `
        -Bound $PSBoundParameters
}

function Get-IseDcTacacsAuth {
    <#
    .SYNOPSIS
    Device-administration logins from the last two days.
    .DESCRIPTION
    The TACACS views retain 48 hours regardless of -Last, and they time on
    EPOCH_TIME (seconds) rather than a timestamp column.

    -Failed matches STATUS against Fail*: the wire contract has equality and
    patterns but no negation, and the exporter's own rule is that anything not
    beginning PASS is a failure.
    .PARAMETER User
    Administrator name; wildcards accepted.
    .PARAMETER Nad
    Network device name; wildcards accepted.
    .PARAMETER Failed
    Only failed logins.
    .EXAMPLE
    Get-IseDcTacacsAuth -Failed -Last 1d
    #>
    [CmdletBinding()]
    param(
        [string]$User,
        [string]$Nad,
        [switch]$Failed,
        [string]$Last,
        [int]$First,
        [string[]]$Column,
        [switch]$Wait,
        [switch]$Force
    )
    $filter = @{}
    $match = @{}
    if ($User) { Add-IseDcTerm $filter $match 'USERNAME' $User }
    if ($Nad) { Add-IseDcTerm $filter $match 'DEVICE_NAME' $Nad }
    if ($Failed) { $match['STATUS'] = 'Fail*' }
    Invoke-IseDcTypedQuery -View 'tacacs_authentication_last_two_days' -Filter $filter `
        -Match $match -Bound $PSBoundParameters
}

function Get-IseDcTacacsCommand {
    <#
    .SYNOPSIS
    What administrators actually typed on the network devices.
    .PARAMETER User
    Administrator name; wildcards accepted.
    .PARAMETER Nad
    Network device name; wildcards accepted.
    .PARAMETER Command
    Command as recorded; wildcards accepted. The arguments are a separate
    column, so 'conf*' matches `configure` and not `show configuration`.
    .EXAMPLE
    Get-IseDcTacacsCommand -User jdoe -Last 1d
    .EXAMPLE
    Get-IseDcTacacsCommand -Command 'reload*'
    #>
    [CmdletBinding()]
    param(
        [string]$User,
        [string]$Nad,
        [string]$Command,
        [string]$Last,
        [int]$First,
        [string[]]$Column,
        [switch]$Wait,
        [switch]$Force
    )
    $filter = @{}
    $match = @{}
    if ($User) { Add-IseDcTerm $filter $match 'USERNAME' $User }
    if ($Nad) { Add-IseDcTerm $filter $match 'DEVICE_NAME' $Nad }
    if ($Command) { Add-IseDcTerm $filter $match 'COMMAND' $Command }
    Invoke-IseDcTypedQuery -View 'tacacs_accounting_last_two_days' -Filter $filter `
        -Match $match -Bound $PSBoundParameters
}

function Get-IseDcTacacsAuthorization {
    <#
    .SYNOPSIS
    Which shell profile and command set a device-administration session matched.
    .PARAMETER User
    Administrator name; wildcards accepted.
    .PARAMETER Nad
    Network device name; wildcards accepted.
    .EXAMPLE
    Get-IseDcTacacsAuthorization -User jdoe -Last 1d
    #>
    [CmdletBinding()]
    param(
        [string]$User,
        [string]$Nad,
        [string]$Last,
        [int]$First,
        [string[]]$Column,
        [switch]$Wait,
        [switch]$Force
    )
    $filter = @{}
    $match = @{}
    if ($User) { Add-IseDcTerm $filter $match 'USERNAME' $User }
    if ($Nad) { Add-IseDcTerm $filter $match 'DEVICE_NAME' $Nad }
    Invoke-IseDcTypedQuery -View 'tacacs_authorization_last_two_days' -Filter $filter `
        -Match $match -Bound $PSBoundParameters
}

function Get-IseDcPosture {
    <#
    .SYNOPSIS
    Posture assessments per endpoint: status, policy matched, agent version.
    .DESCRIPTION
    This view keys on ENDPOINT_MAC_ADDRESS, where the by-condition view says
    ENDPOINT_ID; the two posture views really do disagree, so -Mac is the safe
    way to ask.
    .PARAMETER Mac
    Endpoint MAC; wildcards accepted.
    .PARAMETER Status
    Posture status, e.g. Compliant, NonCompliant, NotApplicable.
    .EXAMPLE
    Get-IseDcPosture -Status NonCompliant -Last 1d
    #>
    [CmdletBinding()]
    param(
        [string]$Mac,
        [string]$Status,
        [string]$Last,
        [int]$First,
        [string[]]$Column,
        [switch]$Wait,
        [switch]$Force
    )
    $filter = @{}
    $match = @{}
    if ($Mac) { Add-IseDcTerm $filter $match 'ENDPOINT_MAC_ADDRESS' $Mac }
    if ($Status) { Add-IseDcTerm $filter $match 'POSTURE_STATUS' $Status }
    Invoke-IseDcTypedQuery -View 'posture_assessment_by_endpoint' -Filter $filter `
        -Match $match -Bound $PSBoundParameters
}

function Get-IseDcNodeHealth {
    <#
    .SYNOPSIS
    Per-node CPU, memory, and filesystem utilisation from the reporting views.
    .PARAMETER Node
    ISE node name; wildcards accepted.
    .EXAMPLE
    Get-IseDcNodeHealth -Last 1h
    #>
    [CmdletBinding()]
    param(
        [string]$Node,
        [string]$Last,
        [int]$First,
        [string[]]$Column,
        [switch]$Wait,
        [switch]$Force
    )
    $filter = @{}
    $match = @{}
    if ($Node) { Add-IseDcTerm $filter $match 'ISE_NODE' $Node }
    Invoke-IseDcTypedQuery -View 'system_summary' -Filter $filter -Match $match `
        -Bound $PSBoundParameters
}

function Get-IseDcNodePerformance {
    <#
    .SYNOPSIS
    Per-node RADIUS load: requests, TPS, latency, noise, and suppression.
    .DESCRIPTION
    This view times on LOGGED_TIME and has no TIMESTAMP column at all, which is
    the sort of detail -Last exists to keep out of an operator's way.
    .PARAMETER Node
    ISE node name; wildcards accepted.
    .EXAMPLE
    Get-IseDcNodePerformance -Node laba-ise-002 -Last 6h
    #>
    [CmdletBinding()]
    param(
        [string]$Node,
        [string]$Last,
        [int]$First,
        [string[]]$Column,
        [switch]$Wait,
        [switch]$Force
    )
    $filter = @{}
    $match = @{}
    if ($Node) { Add-IseDcTerm $filter $match 'ISE_NODE' $Node }
    Invoke-IseDcTypedQuery -View 'key_performance_metrics' -Filter $filter -Match $match `
        -Bound $PSBoundParameters
}

# Completion reads the cached descriptors, so it costs nothing and stays silent
# when the exporter is not up: a shell that cannot complete is an inconvenience,
# one that throws on every Tab is unusable.
$completeView = {
    param($commandName, $parameterName, $wordToComplete, $commandAst, $fakeBoundParameters)
    try {
        $pattern = "$wordToComplete*"
        @(Get-IseDcViewCache) | Where-Object { $_.name -like $pattern } | ForEach-Object {
            [System.Management.Automation.CompletionResult]::new(
                $_.name, $_.name, 'ParameterValue',
                "$($_.view): $($_.description)")
        }
    }
    catch { }
}

$completeColumn = {
    param($commandName, $parameterName, $wordToComplete, $commandAst, $fakeBoundParameters)
    try {
        $view = $fakeBoundParameters['View']
        if (-not $view) { return }
        $pattern = "$wordToComplete*"
        @(Get-IseDcViewCache) |
            Where-Object { $_.name -eq $view } |
            ForEach-Object { $_.columns } |
            Where-Object { $_ -like $pattern } |
            ForEach-Object {
                [System.Management.Automation.CompletionResult]::new(
                    $_, $_, 'ParameterValue', $_)
            }
    }
    catch { }
}

Register-ArgumentCompleter -CommandName Invoke-IseDcQuery -ParameterName View -ScriptBlock $completeView
Register-ArgumentCompleter -CommandName Get-IseDcColumn -ParameterName View -ScriptBlock $completeView
Register-ArgumentCompleter -CommandName Get-IseDcView -ParameterName Name -ScriptBlock $completeView
Register-ArgumentCompleter -CommandName Invoke-IseDcQuery -ParameterName Column -ScriptBlock $completeColumn
Register-ArgumentCompleter -CommandName Invoke-IseDcQuery -ParameterName OrderBy -ScriptBlock $completeColumn

Export-ModuleMember -Function @(
    'Get-IseApiRoot', 'Set-IseApiRoot', 'Invoke-IseApi',
    'Get-IseHealth', 'Get-IseDataset', 'Get-IseProvider',
    'Get-IseTarget', 'Get-IsePlan', 'Get-IseDegraded',
    'Get-IseDcView', 'Get-IseDcColumn', 'Get-IseDcStatus', 'Invoke-IseDcQuery',
    'Get-IseDcRadiusAuth', 'Get-IseDcRadiusAccounting', 'Get-IseDcRadiusError',
    'Get-IseDcEndpoint', 'Get-IseDcTacacsAuth', 'Get-IseDcTacacsCommand',
    'Get-IseDcTacacsAuthorization', 'Get-IseDcPosture', 'Get-IseDcNodeHealth',
    'Get-IseDcNodePerformance'
)
