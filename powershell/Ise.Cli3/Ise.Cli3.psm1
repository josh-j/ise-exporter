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

# Shipped inside the module rather than under docs/, which is not installed and
# not even in the repository: a guide that only exists in a working checkout is
# not there for the operator on the appliance host at 3am, which is the only
# time anybody reads it.
$script:IseReadmePath = Join-Path $PSScriptRoot 'Ise.Cli3.Readme.md'

function Get-IseMarkdownStyle {
    <#
    .SYNOPSIS
    ANSI codes when the terminal wants them, empty strings when it does not.
    #>
    param()

    $plain = [pscustomobject]@{
        Head = ''; Sub = ''; Code = ''; Rule = ''; Emphasis = ''; Reset = ''
    }
    if ($env:NO_COLOR) { return $plain }
    if (-not $Host.UI.SupportsVirtualTerminal) { return $plain }
    $e = [char]27
    [pscustomobject]@{
        Head     = "$e[1;36m"      # section headings
        Sub      = "$e[1m"         # sub-headings and bold runs
        Code     = "$e[38;5;150m"  # command lines and inline literals
        Rule     = "$e[38;5;240m"  # table rules, the quiet furniture
        Emphasis = "$e[38;5;180m"  # table headers
        Reset    = "$e[0m"
    }
}

function Format-IseMarkdownInline {
    <#
    .SYNOPSIS
    Strip the markers a terminal cannot use, keep what they meant.
    #>
    param([string]$Text, $Style)

    # Links first: the label is the readable part and the URL is noise in a
    # shell, except when there is no label to keep.
    $out = [regex]::Replace($Text, '\[([^\]]+)\]\(([^)]+)\)', '$1 ($2)')
    $out = [regex]::Replace($out, '\*\*([^*]+)\*\*',
        { param($m) "$($Style.Sub)$($m.Groups[1].Value)$($Style.Reset)" })
    $out = [regex]::Replace($out, '`([^`]+)`',
        { param($m) "$($Style.Code)$($m.Groups[1].Value)$($Style.Reset)" })
    $out
}

function Get-IseVisibleLength {
    # Column widths have to be measured in what the eye sees, not in what the
    # escape sequences add.
    param([string]$Text)
    ([regex]::Replace($Text, "$([char]27)\[[0-9;]*m", '')).Length
}

function Format-IseWrapped {
    param([string]$Text, [int]$Width, [string]$Indent = '')

    $words = $Text -split '\s+' | Where-Object { $_ }
    if (-not $words) { return @('') }
    $lines, $current = @(), $Indent
    foreach ($word in $words) {
        $candidate = if ($current -eq $Indent) { "$current$word" } else { "$current $word" }
        if ((Get-IseVisibleLength $candidate) -gt $Width -and $current -ne $Indent) {
            $lines += $current
            $current = "$Indent$word"
        }
        else { $current = $candidate }
    }
    @($lines + $current)
}

function Format-IseMarkdown {
    <#
    .SYNOPSIS
    Render the guide for a terminal: wrapped prose, aligned tables, quiet code.
    .DESCRIPTION
    Show-Markdown exists and was tried first. It collapses every table onto one
    line with the pipes and dashes intact, emits cursor-movement escapes inside
    fenced code, and never wraps a paragraph -- so the parts of this guide that
    carry the most information are the parts it renders worst. This does the
    four things that actually matter for reading a reference in a shell.
    #>
    param([string[]]$Line, [int]$Width = 0)

    $style = Get-IseMarkdownStyle
    if ($Width -le 0) {
        $Width = try { [Console]::WindowWidth - 2 } catch { 96 }
    }
    # Long measures are hard to read even on a wide terminal, and unreadably
    # hard on a maximised one.
    $Width = [math]::Max(48, [math]::Min($Width, 96))

    $out = [System.Collections.Generic.List[string]]::new()
    $index = 0
    while ($index -lt $Line.Count) {
        $text = $Line[$index]

        if ($text -match '^```') {
            # Code is quoted, not reflowed: a wrapped command is a broken one.
            $index++
            while ($index -lt $Line.Count -and $Line[$index] -notmatch '^```') {
                $out.Add("  $($style.Code)$($Line[$index])$($style.Reset)")
                $index++
            }
            $index++
            $out.Add('')
            continue
        }

        if ($text -match '^\s*\|') {
            $rows = @()
            while ($index -lt $Line.Count -and $Line[$index] -match '^\s*\|') {
                $cells = $Line[$index].Trim() -replace '^\|', '' -replace '\|$', ''
                $rows += , @($cells -split '\|' | ForEach-Object { $_.Trim() })
                $index++
            }
            # The |---|---| rule carries no content; the alignment it describes
            # is rebuilt below from the real widths.
            $body = @($rows | Where-Object {
                ($_ -join '') -notmatch '^[-: ]+$' })
            if ($body.Count) {
                $columns = ($body | ForEach-Object { $_.Count } |
                    Measure-Object -Maximum).Maximum
                $rendered = foreach ($row in $body) {
                    , @(for ($c = 0; $c -lt $columns; $c++) {
                        Format-IseMarkdownInline ([string]$row[$c]) $style })
                }
                $widths = for ($c = 0; $c -lt $columns; $c++) {
                    ($rendered | ForEach-Object {
                        Get-IseVisibleLength $_[$c] } |
                        Measure-Object -Maximum).Maximum
                }
                $natural = (($widths | Measure-Object -Sum).Sum) +
                    (2 * ($columns - 1)) + 2
                if ($natural -le $Width) {
                    $first = $true
                    foreach ($row in $rendered) {
                        $cells = for ($c = 0; $c -lt $columns; $c++) {
                            $pad = $widths[$c] - (Get-IseVisibleLength $row[$c])
                            "$($row[$c])$(' ' * [math]::Max(0, $pad))"
                        }
                        $prefix = if ($first) { $style.Emphasis } else { '' }
                        $suffix = if ($first) { $style.Reset } else { '' }
                        $out.Add("  $prefix$(($cells -join '  ').TrimEnd())$suffix")
                        if ($first) {
                            $rule = ($widths | ForEach-Object { '-' * $_ }) -join '  '
                            $out.Add("  $($style.Rule)$rule$($style.Reset)")
                            $first = $false
                        }
                    }
                }
                else {
                    # Too wide to align. Squeezing the columns would wrap every
                    # cell into an unreadable stack; turning each row into a
                    # labelled block keeps it readable at any width, which is
                    # what a comparison table is for.
                    $labels = $rendered[0]
                    $label = ($labels[1..($columns - 1)] | ForEach-Object {
                        Get-IseVisibleLength $_ } | Measure-Object -Maximum).Maximum
                    foreach ($row in $rendered[1..($rendered.Count - 1)]) {
                        $out.Add("  $($style.Sub)$($row[0])$($style.Reset)")
                        for ($c = 1; $c -lt $columns; $c++) {
                            if (-not $row[$c]) { continue }
                            $pad = $label - (Get-IseVisibleLength $labels[$c])
                            $lead = "    $($labels[$c])$(' ' * [math]::Max(0, $pad))  "
                            $wrapped = @(Format-IseWrapped $row[$c] $Width (
                                ' ' * (Get-IseVisibleLength $lead)))
                            $wrapped[0] = $lead + $wrapped[0].TrimStart()
                            $out.AddRange([string[]]$wrapped)
                        }
                        $out.Add('')
                    }
                }
                $out.Add('')
            }
            continue
        }

        if ($text -match '^(#{1,6})\s+(.*)$') {
            $depth = $matches[1].Length
            $title = Format-IseMarkdownInline $matches[2] $style
            if ($out.Count -and $out[$out.Count - 1] -ne '') { $out.Add('') }
            if ($depth -le 2) {
                $out.Add("$($style.Head)$title$($style.Reset)")
                $out.Add("$($style.Rule)$('-' * (Get-IseVisibleLength $title))$($style.Reset)")
            }
            else {
                $out.Add("$($style.Sub)$title$($style.Reset)")
            }
            $out.Add('')
            $index++
            continue
        }

        if ($text -match '^\s*$') {
            if ($out.Count -and $out[$out.Count - 1] -ne '') { $out.Add('') }
            $index++
            continue
        }

        if ($text -match '^(\s*)([-*+]|\d+\.)\s+(.*)$') {
            $lead = "$($matches[1])  "
            $body = Format-IseMarkdownInline $matches[3] $style
            $wrapped = @(Format-IseWrapped $body ($Width - $lead.Length - 2) "$lead  ")
            $wrapped[0] = "$lead* " + $wrapped[0].TrimStart()
            $out.AddRange([string[]]$wrapped)
            $index++
            continue
        }

        # A paragraph is every following non-blank, non-structural line.
        $paragraph = @()
        while ($index -lt $Line.Count -and $Line[$index] -notmatch '^\s*$' -and
               $Line[$index] -notmatch '^(#{1,6}\s|```|\s*\||\s*([-*+]|\d+\.)\s)') {
            $paragraph += $Line[$index].Trim()
            $index++
        }
        $joined = Format-IseMarkdownInline ($paragraph -join ' ') $style
        $out.AddRange([string[]](Format-IseWrapped $joined $Width '  '))
    }
    $out
}

function Get-IseCliReadme {
    <#
    .SYNOPSIS
    The ise-cli3 operator guide, without leaving the shell.
    .DESCRIPTION
    Prints the guide that ships with this module: what costs Oracle duty cycle
    and what is free, how filters and windows travel, what a refusal means, and
    what to do about it.

    Whole thing by default. -Section prints one part, matched on its heading and
    accepting wildcards, because the answer wanted mid-incident is one section
    and not eleven. -List names the sections without printing any of them.

    Rendered for a terminal: prose wrapped to the window, tables aligned,
    commands set apart. -Raw emits the markdown source instead, for piping
    somewhere that would rather do its own formatting.
    .PARAMETER Section
    Heading to print; wildcards accepted, matched case-insensitively. A pattern
    matching several headings prints all of them, in document order.
    .PARAMETER List
    List the section headings instead of printing anything.
    .PARAMETER Raw
    Emit the markdown source rather than the rendered form.
    .PARAMETER Width
    Wrap at this many columns instead of the window width.
    .EXAMPLE
    Get-IseCliReadme
    .EXAMPLE
    Get-IseCliReadme -List
    .EXAMPLE
    Get-IseCliReadme -Section 'refused'
    .EXAMPLE
    Get-IseCliReadme | Out-Host -Paging
    .EXAMPLE
    Get-IseCliReadme -Raw | Out-File ise-cli3.md
    #>
    [CmdletBinding(DefaultParameterSetName = 'Whole')]
    param(
        [Parameter(ParameterSetName = 'Section', Position = 0)][string]$Section,
        [Parameter(ParameterSetName = 'List')][switch]$List,
        [switch]$Raw,
        [int]$Width = 0
    )

    if (-not (Test-Path -LiteralPath $script:IseReadmePath)) {
        throw [System.IO.FileNotFoundException]::new(
            "The ise-cli3 guide is missing from this install: $script:IseReadmePath. " +
            'Re-run deploy/install.sh to repair the module directory.',
            $script:IseReadmePath)
    }
    $lines = @(Get-Content -LiteralPath $script:IseReadmePath)

    # Sections are the '## ' headings. The title above the first one is the
    # preamble, which belongs to the whole document and to no section.
    $headings = @($lines | Where-Object { $_ -match '^##\s+' } |
        ForEach-Object { $_ -replace '^##\s+', '' })

    if ($List) { return $headings }
    if (-not $Section) {
        return $(if ($Raw) { $lines -join [Environment]::NewLine }
                 else { (Format-IseMarkdown $lines $Width) -join [Environment]::NewLine })
    }

    $pattern = if ($Section -match '[*?]') { $Section } else { "*$Section*" }
    $wanted = @($headings | Where-Object { $_ -like $pattern })
    if (-not $wanted.Count) {
        throw [System.ArgumentException]::new(
            "No section matches '$Section'. Get-IseCliReadme -List names them.")
    }

    $out = [System.Collections.Generic.List[string]]::new()
    $keeping = $false
    foreach ($line in $lines) {
        if ($line -match '^##\s+') {
            $keeping = $wanted -contains ($line -replace '^##\s+', '')
        }
        if ($keeping) { $out.Add($line) }
    }
    if ($Raw) { return $out -join [Environment]::NewLine }
    (Format-IseMarkdown $out $Width) -join [Environment]::NewLine
}

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
        'pxgrid_unconfigured' {
            'This exporter has no pxGrid target configured, so there is no ' +
            'session directory to read. Use -WithLastAuth to take the session ' +
            'context from Data Connect instead.'
        }
        'connection_failed' {
            # The one refusal that is about the far end rather than a guard.
            'The exporter could not reach the source: ' +
            'Get-IseDataset -Unhealthy shows what else it is affecting.'
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
    .PARAMETER All
    Show every column the rows carry rather than the view's curated table.

    The exporter already returns the whole row; what narrows it is this module's
    format table, which names a handful of columns per view so the default
    output reads at a glance in an 80-column terminal. That is a display choice,
    and -All is how to decline it: the rows come back untyped, so PowerShell
    formats them generically and every column is visible. Costs nothing extra --
    same statement, same duty cycle, same rows.
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

    Not needed for a lookup. A keyed read of a current-state view -- one exact
    filter, at most 25 rows, no window, no grouping -- is already served through
    a cooldown, and pays its full charge on top of the outstanding one rather
    than taking the discount this switch does. Cutting in is bounded: once
    lookups have pushed the shared deadline out by about a minute they queue
    like anything else, so a script in a loop cannot starve the scheduler.
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
        [switch]$All,
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
    # PowerShell show what actually came back. -All asks for the same escape for
    # ordinary rows: format lookup takes the first type name that has a view, so
    # withholding the curated one is the only way to stop it narrowing -- there
    # is no ps1xml for "every property". Both land on a type with no format view
    # of its own, which is exactly what makes PowerShell print all of it.
    $typeName = if ($GroupBy -or $Aggregate) { 'Ise.Dc.Grouped' }
                elseif ($All) { 'Ise.Dc.Row' }
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
    foreach ($name in @('Last', 'First', 'Column', 'All', 'Wait', 'Force')) {
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
        [switch]$All,
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
        [switch]$All,
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
        [switch]$All,
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
        [switch]$All,
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
        [switch]$All,
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
        [switch]$All,
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
        [switch]$All,
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
        [switch]$All,
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
        [switch]$All,
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
        [switch]$All,
        [switch]$Wait,
        [switch]$Force
    )
    $filter = @{}
    $match = @{}
    if ($Node) { Add-IseDcTerm $filter $match 'ISE_NODE' $Node }
    Invoke-IseDcTypedQuery -View 'key_performance_metrics' -Filter $filter -Match $match `
        -Bound $PSBoundParameters
}

# --- screen replicas ---------------------------------------------------------
#
# Two cmdlets that answer the questions the ISE web UI answers, in the shape the
# UI answers them. They are ordinary Data Connect reads -- same paced transport,
# same duty cycle, same ceilings -- so what makes them replicas is the column
# set and the ordering, not a different data path. Whoever knows the screen
# should recognise the table without reading the help.
#
# Neither refreshes on a timer. The UI does; this deliberately does not, because
# a poll loop against a duty-cycled Oracle account spends the whole reporting
# budget for as long as it is left running, and the datasets that share it would
# go stale to feed a screen nobody is watching.

function ConvertTo-IseMacKey {
    <#
    .SYNOPSIS
    A MAC reduced to the only part every ISE column spells the same way.
    #>
    param([string]$Value)

    # ENDPOINTS_DATA writes AA:BB:CC:11:22:33; CALLING_STATION_ID carries
    # whatever the NAD sent, which is that, or AA-BB-CC-11-22-33, or
    # aabb.cc11.2233, sometimes with a suffix. Joining on the raw strings drops
    # rows for no reason an operator could see, so both sides reduce to hex.
    if ([string]::IsNullOrWhiteSpace($Value)) { return '' }
    ($Value -replace '[^0-9A-Fa-f]', '').ToUpperInvariant()
}

function Get-IseRadiusLiveLog {
    <#
    .SYNOPSIS
    RADIUS Live Logs: authentication attempts newest-first, as the UI lists them.
    .DESCRIPTION
    The Operations > RADIUS > Live Logs table, from RADIUS_AUTHENTICATIONS. Same
    columns in the same order -- time, status, identity, endpoint, endpoint
    profile, authorization profiles, network device, server, failure reason --
    and the same newest-first ordering, which is the whole point of the screen.

    Status is derived, not stored: this view carries the FAILED flag rather than
    a status string, so `status` is the word for that flag and `failed` is still
    on the row for anything that would rather test the number.

    It is a snapshot, not a feed. There is no auto-refresh, because refreshing
    on the UI's cadence would spend the entire Oracle duty cycle on one terminal
    and starve every scheduled dataset sharing it. Run it again when you want a
    newer answer; -Wait sits out the cooldown if one is owed.
    .PARAMETER Identity
    The user's claimed identity (USERNAME); wildcards accepted.
    .PARAMETER Endpoint
    Endpoint ID -- the MAC as the NAD sent it (CALLING_STATION_ID); wildcards
    accepted.
    .PARAMETER Nad
    Network device name; wildcards accepted.
    .PARAMETER Node
    The ISE node that served the request (the UI's Server column); wildcards
    accepted.
    .PARAMETER Status
    Pass, Fail, or All. Defaults to All, as the screen does.
    .PARAMETER AuthzProfile
    Authorization profile the result carried; wildcards accepted.
    .PARAMETER FailureReason
    Failure reason text; wildcards accepted. Implies -Status Fail.
    .EXAMPLE
    Get-IseRadiusLiveLog -Last 1h
    .EXAMPLE
    Get-IseRadiusLiveLog -Status Fail -Last 4h | Group-Object failure_reason
    .EXAMPLE
    Get-IseRadiusLiveLog -Nad 'core-*' -Last 30m -First 200
    #>
    [CmdletBinding()]
    param(
        [string]$Identity,
        [string]$Endpoint,
        [string]$Nad,
        [string]$Node,
        [ValidateSet('All', 'Pass', 'Fail')][string]$Status = 'All',
        [string]$AuthzProfile,
        [string]$FailureReason,
        [string]$Last,
        [int]$First,
        [string[]]$Column,
        [switch]$All,
        [switch]$Wait,
        [switch]$Force
    )

    $view = 'radius_authentications'
    $filter = @{}
    $match = @{}
    if ($Identity) {
        Add-IseDcTerm $filter $match (Resolve-IseDcColumn $view 'USERNAME', 'USER_NAME') $Identity
    }
    if ($Endpoint) {
        Add-IseDcTerm $filter $match (
            Resolve-IseDcColumn $view 'CALLING_STATION_ID', 'MAC_ADDRESS') $Endpoint
    }
    if ($Nad) { Add-IseDcTerm $filter $match 'DEVICE_NAME' $Nad }
    if ($Node) { Add-IseDcTerm $filter $match 'ISE_NODE' $Node }
    if ($AuthzProfile) { Add-IseDcTerm $filter $match 'AUTHORIZATION_PROFILES' $AuthzProfile }
    if ($FailureReason) {
        Add-IseDcTerm $filter $match 'FAILURE_REASON' $FailureReason
        # A failure reason only exists on a failure; saying so server-side saves
        # scanning the passes to discard them.
        $Status = 'Fail'
    }
    if ($Status -eq 'Fail') { $filter['FAILED'] = '1' }
    if ($Status -eq 'Pass') { $filter['FAILED'] = '0' }

    $arguments = @{ View = $view }
    if ($filter.Count) { $arguments['Filter'] = $filter }
    if ($match.Count) { $arguments['Match'] = $match }
    foreach ($name in @('Last', 'First', 'Column', 'All', 'Wait', 'Force')) {
        if ($PSBoundParameters.ContainsKey($name)) { $arguments[$name] = $PSBoundParameters[$name] }
    }
    # Newest first is what makes it a log rather than a query result. Ordered
    # server-side so the row cap keeps the newest rows, not an arbitrary page.
    $arguments['OrderBy'] = Resolve-IseDcColumn $view 'TIMESTAMP', 'TIMESTAMP_TIMEZONE'
    $arguments['Descending'] = $true

    foreach ($row in @(Invoke-IseDcQuery @arguments)) {
        if ($null -eq $row) { continue }
        # Null rather than a guess when the projection left FAILED out: an
        # unknown status must not read as a pass.
        $state = $null
        if ($null -ne $row.failed) {
            $state = if ([int]$row.failed -ne 0) { 'Fail' } else { 'Pass' }
        }
        Add-Member -InputObject $row -Force -NotePropertyName 'status' -NotePropertyValue $state
        # -All asked for the untyped row on purpose; re-typing it here would put
        # a format table back and undo exactly what was asked for.
        if (-not $All) { $row.PSObject.TypeNames.Insert(0, 'Ise.Dc.RadiusLiveLog') }
        $row
    }
}

function Get-IseContextVisibility {
    <#
    .SYNOPSIS
    Context Visibility: the endpoint database as the Endpoints screen shows it.
    .DESCRIPTION
    The Context Visibility > Endpoints table, from ENDPOINTS_DATA: MAC, IP,
    hostname, endpoint profile, identity group, registration and static
    assignment, and when the row last changed.

    Current state, not history -- without -Last it returns the whole database up
    to -First, which on a real deployment is tens of thousands of rows, so -First
    matters here more than anywhere else. With -Last it narrows to endpoints
    whose row changed inside the window.

    Three sources can be attached, and they are nearly disjoint, so they add
    rather than compete. Measured on a live appliance: 50 of an endpoint's 53
    readable probe attributes have no pxGrid counterpart, and 28 of pxGrid's 31
    session fields have no probe counterpart. -Full turns all three on.

    -WithProbe merges everything ISE profiled about the endpoint: AD resolution,
    registration state, OUI, certainty, DHCP and the rest, as probe_* columns.
    It costs nothing extra -- PROBE_DATA is already in the row this cmdlet
    fetches, and was being discarded. Cisco's view truncates it, so a busy
    endpoint's attribute set arrives partial and says so.

    -WithLastAuth adds the screen's Authentication tab as auth_* columns, from
    each endpoint's most recent RADIUS authentication. That is a second
    statement against a second view, so it charges the duty cycle twice, and it
    only reaches endpoints that authenticated inside the auth window.

    -ViaPxGrid adds live session state as session_* columns, free. Between them
    they answer different questions: auth_* is the last authentication whenever
    it happened, session_* is what is connected now.

    Whatever supplied them, `identity` and `nad` are set from whichever source
    had one, so the table reads the same under any combination.
    .PARAMETER Mac
    MAC address; wildcards accepted.
    .PARAMETER Ip
    Endpoint IP address; wildcards accepted.
    .PARAMETER Hostname
    Endpoint hostname; wildcards accepted.
    .PARAMETER Profile
    Endpoint policy -- the profile ISE matched; wildcards accepted.
    .PARAMETER User
    Portal user recorded on the endpoint; wildcards accepted.
    .PARAMETER Last
    Only endpoints whose row changed inside this window: 30m, 2h, 1d.
    .PARAMETER WithProbe
    Merge the endpoint's profiling attributes as probe_* columns. Free: the
    column is already fetched.
    .PARAMETER WithLastAuth
    Attach each endpoint's most recent authentication as auth_* columns. Costs
    a second statement.
    .PARAMETER AuthLast
    Window for the authentication lookup -WithLastAuth performs. Defaults to 1d.
    .PARAMETER ViaPxGrid
    Attach live session state as session_* columns, from pxGrid's session
    directory. Free -- the exporter already holds the snapshot, kept current by
    the subscription active_sessions pays for -- and live rather than "most
    recent inside a window". What it cannot do is answer for an endpoint that is
    not connected. Requires a configured pxGrid target; without one the cmdlet
    says so rather than silently returning nothing.
    .PARAMETER WithSession
    Attach MnT's session detail as mnt_* columns: accounting counters, the
    policy execution steps, the correlation ids, and the authorization detail.
    39 of its 47 fields have no counterpart in any other source.

    MnT has no bulk form, so this is one request per endpoint against the PAN
    budget -- not the Oracle duty cycle, but a real budget all the same. It
    therefore refuses rather than scales: past -SessionLimit endpoints it stops
    and says so instead of issuing hundreds of requests nobody asked for. Meant
    for one endpoint, or a handful.
    .PARAMETER SessionLimit
    How many endpoints -WithSession will fetch detail for. Defaults to 25.
    .PARAMETER Full
    Every source this cmdlet can reach: -WithProbe, -WithLastAuth, -ViaPxGrid.
    Not -WithSession, which is per-endpoint and has to be asked for.
    .EXAMPLE
    Get-IseContextVisibility -Profile 'Cisco-IP-Phone*' -First 500
    .EXAMPLE
    Get-IseContextVisibility -Last 1h -WithLastAuth
    .EXAMPLE
    Get-IseContextVisibility -Mac 'AA:BB:CC:*' -WithLastAuth | Format-List *
    #>
    [CmdletBinding()]
    param(
        [string]$Mac,
        [string]$Ip,
        [string]$Hostname,
        [string]$Profile,
        [string]$User,
        [switch]$WithProbe,
        [switch]$WithLastAuth,
        [switch]$ViaPxGrid,
        [switch]$WithSession,
        [int]$SessionLimit = 25,
        [switch]$Full,
        [string]$AuthLast = '1d',
        [string]$Last,
        [int]$First,
        [string[]]$Column,
        [switch]$All,
        [switch]$Wait,
        [switch]$Force
    )

    $view = 'endpoints_data'
    $filter = @{}
    $match = @{}
    if ($Mac) { Add-IseDcTerm $filter $match 'MAC_ADDRESS' $Mac }
    if ($Ip) { Add-IseDcTerm $filter $match 'ENDPOINT_IP' $Ip }
    if ($Hostname) { Add-IseDcTerm $filter $match 'HOSTNAME' $Hostname }
    if ($Profile) { Add-IseDcTerm $filter $match 'ENDPOINT_POLICY' $Profile }
    if ($User) { Add-IseDcTerm $filter $match 'PORTAL_USER' $User }

    $endpoints = @(Invoke-IseDcTypedQuery -View $view -Filter $filter -Match $match `
        -Bound $PSBoundParameters)

    # They no longer collide, so there is nothing to refuse: auth_* is the last
    # authentication whenever it happened, session_* is what is connected now,
    # probe_* is what ISE profiled. Asking for all three is the point of -Full.
    if ($Full) { $WithProbe = $WithLastAuth = $ViaPxGrid = $true }
    # Counted rather than capped after the fact: the refusal has to happen
    # before the requests, not be discovered in the output afterwards.
    $fetched = 0

    # pxGrid first, because it is the free one. The exporter is holding this
    # snapshot already; reading it spends no Oracle duty cycle and no cooldown,
    # which is the whole reason to prefer it when an endpoint is connected.
    $lastAuth = @{}
    $sessions = @{}
    if ($ViaPxGrid -and $endpoints.Count) {
        $query = @{}
        if ($Mac) { $query['mac'] = $Mac }
        # Deliberately not -First. That bounds the *endpoint* rows; the session
        # list is a different set, sorted by MAC and cut at the ceiling, so
        # sending 50 here would fetch the 50 lowest-MAC sessions on the
        # appliance rather than the sessions belonging to these 50 endpoints --
        # and every endpoint sorting past the cut would silently come back with
        # empty session_* columns. The route's own ceiling is the right bound,
        # and reading memory the exporter already holds costs nothing to widen.
        $answer = Invoke-IseApi -Path '/api/v1/pxgrid/sessions' -Query $query
        if ($answer.truncated) {
            Write-Warning ("pxGrid holds $($answer.matched) sessions and this read " +
                           "kept $($answer.row_count); endpoints whose session was " +
                           'left behind will show empty session_* columns. Narrow ' +
                           'with -Mac to bring them into range.')
        }
        foreach ($session in @($answer.sessions)) {
            if ($null -eq $session) { continue }
            $key = ConvertTo-IseMacKey $session.mac_address
            if ($key -and -not $sessions.ContainsKey($key)) { $sessions[$key] = $session }
        }
    }

    # One statement for the authentications, not one per endpoint: the query API
    # binds a single value per column, so N endpoints would be N statements and
    # N cooldowns. Newest-first over the window, keep the first sighting of each
    # MAC, and join in memory.
    if ($WithLastAuth -and $endpoints.Count) {
        $authArguments = @{
            View = 'radius_authentications'; Last = $AuthLast
            OrderBy = Resolve-IseDcColumn 'radius_authentications' 'TIMESTAMP'
            Descending = $true
        }
        foreach ($name in @('First', 'Wait', 'Force')) {
            if ($PSBoundParameters.ContainsKey($name)) {
                $authArguments[$name] = $PSBoundParameters[$name]
            }
        }
        foreach ($auth in @(Invoke-IseDcQuery @authArguments)) {
            if ($null -eq $auth) { continue }
            $key = ConvertTo-IseMacKey $auth.calling_station_id
            # Newest first, so the first row seen for a MAC is its latest and
            # every later one is history this screen does not show.
            if ($key -and -not $lastAuth.ContainsKey($key)) { $lastAuth[$key] = $auth }
        }
    }

    foreach ($row in $endpoints) {
        if ($null -eq $row) { continue }
        $key = ConvertTo-IseMacKey $row.mac_address

        if ($WithLastAuth) {
            $auth = $lastAuth[$key]
            # Written even when nothing matched: a column that exists and is
            # empty says "this endpoint did not authenticate in the window",
            # while a missing property says nothing at all.
            foreach ($pair in @(
                @('auth_time', $auth.timestamp),
                @('auth_identity', $auth.username),
                @('auth_nad', $auth.device_name),
                @('auth_profiles', $auth.authorization_profiles),
                @('auth_method', $auth.authentication_method),
                @('auth_posture', $auth.posture_status),
                @('auth_node', $auth.ise_node))) {
                Add-Member -InputObject $row -Force `
                    -NotePropertyName $pair[0] -NotePropertyValue $pair[1]
            }
        }

        if ($ViaPxGrid) {
            $session = $sessions[$key]
            # Every projected field, prefixed. Provenance is the point: an
            # operator reading session_nad knows it came from a live session
            # and not from an authentication that may be a day old.
            foreach ($name in @(
                'user_name', 'ip_address', 'nad', 'nas_ip_address', 'nas_port',
                'endpoint_profile', 'auth_method', 'auth_protocol',
                'posture_status', 'authorization_profiles', 'security_group',
                'ise_node', 'session_state', 'audit_session_id', 'last_update')) {
                Add-Member -InputObject $row -Force `
                    -NotePropertyName "session_$name" `
                    -NotePropertyValue $(if ($session) { $session.$name } else { $null })
            }
        }

        if ($WithSession -and $fetched -lt $SessionLimit) {
            $fetched++
            $detail = $null
            try {
                $answer = Invoke-IseApi -Path '/api/v1/mnt/session' `
                    -Query @{ mac = $row.mac_address }
                if ($answer.found) { $detail = $answer.session }
            }
            catch {
                # One endpoint's detail failing is not the whole read failing:
                # say which, and carry on with the rest.
                Write-Warning ("$($row.mac_address): session detail unavailable " +
                               "($($_.Exception.Message))")
            }
            if ($detail) {
                foreach ($field in $detail.PSObject.Properties) {
                    if ([string]::IsNullOrEmpty($field.Value)) { continue }
                    $name = ($field.Name -replace '[^A-Za-z0-9]+', '_').Trim('_')
                    Add-Member -InputObject $row -Force `
                        -NotePropertyName "mnt_$($name.ToLowerInvariant())" `
                        -NotePropertyValue $field.Value
                }
            }
        }

        if ($WithProbe) {
            # Free: PROBE_DATA is already on the row, and was being discarded.
            # Names are normalised because ISE writes 'AD-Join-Point' and
            # 'Total Certainty Factor', neither of which reads well as a
            # PowerShell property; probe_data.attributes keeps the originals.
            $probe = $row.probe_data
            if ($probe -and $probe.attributes) {
                foreach ($attribute in $probe.attributes.PSObject.Properties) {
                    if ([string]::IsNullOrEmpty($attribute.Value)) { continue }
                    $name = ($attribute.Name -replace '[^A-Za-z0-9]+', '_').Trim('_')
                    Add-Member -InputObject $row -Force `
                        -NotePropertyName "probe_$($name.ToLowerInvariant())" `
                        -NotePropertyValue $attribute.Value
                }
            }
            if ($probe -and $probe.truncated) {
                Write-Warning "$($row.mac_address): $($probe.note)"
            }
        }

        # One pair of columns the table can always name, whichever source
        # happened to supply them. Without this the default table goes blank
        # the moment somebody swaps -WithLastAuth for -ViaPxGrid.
        if ($WithLastAuth -or $ViaPxGrid -or $WithProbe -or $WithSession) {
            $identity = @($row.auth_identity, $row.session_user_name,
                          $row.probe_user, $row.portal_user) |
                Where-Object { -not [string]::IsNullOrEmpty($_) } |
                Select-Object -First 1
            $device = @($row.session_nad, $row.auth_nad,
                        $row.probe_networkdevicename) |
                Where-Object { -not [string]::IsNullOrEmpty($_) } |
                Select-Object -First 1
            Add-Member -InputObject $row -Force -NotePropertyName 'identity' `
                -NotePropertyValue $identity
            Add-Member -InputObject $row -Force -NotePropertyName 'nad' `
                -NotePropertyValue $device
        }

        if (-not $All) { $row.PSObject.TypeNames.Insert(0, 'Ise.Dc.ContextVisibility') }
        $row
    }
    if ($WithSession -and $endpoints.Count -gt $SessionLimit) {
        Write-Warning ("MnT has no bulk form, so session detail was fetched for " +
                       "the first $SessionLimit of $($endpoints.Count) endpoints. " +
                       'Narrow the result or raise -SessionLimit.')
    }
}

function Get-IseEndpointProbe {
    <#
    .SYNOPSIS
    Everything ISE profiled about an endpoint, one attribute per row.
    .DESCRIPTION
    ENDPOINTS_DATA.PROBE_DATA holds the profiling attributes as a byte stream,
    which the exporter decodes. This is the readable form: one row per
    attribute, sorted by name, ready for Where-Object and Export-Csv.

    Empty attributes are hidden. ISE writes a name for every attribute it knows
    of whether or not it has a value, so a real endpoint carries dozens of blank
    ones and showing them buries the handful that say something. -IncludeEmpty
    puts them back.

    ISE serialises more attributes than the column can hold, so the answer is
    often a prefix. That is a truncation in the database and not in this shell,
    and the cmdlet warns naming how many were cut rather than presenting a
    partial set as a complete one.

    Only the two columns it needs are fetched, so this is far cheaper than a
    whole-row endpoint read even though it charges the same duty cycle.
    .PARAMETER Mac
    MAC address; wildcards accepted. Without it, every endpoint is read, which
    on a real deployment is why -First exists.
    .PARAMETER Name
    Only attributes whose name matches; wildcards accepted.
    .PARAMETER IncludeEmpty
    Show attributes ISE named but left empty.
    .PARAMETER AsObject
    One object per endpoint with the attributes as properties, instead of one
    row per attribute. Convenient for $probe.OUI; awkward to filter.
    .EXAMPLE
    Get-IseEndpointProbe -Mac 'AA:BB:CC:11:22:33'
    .EXAMPLE
    Get-IseEndpointProbe -Mac 'AA:BB:*' -Name '*MFCInfo*'
    .EXAMPLE
    (Get-IseEndpointProbe -Mac 'AA:BB:CC:11:22:33' -AsObject).OUI
    #>
    [CmdletBinding()]
    param(
        [string]$Mac,
        [string]$Name,
        [switch]$IncludeEmpty,
        [switch]$AsObject,
        [int]$First,
        [switch]$Wait,
        [switch]$Force
    )

    $filter = @{}
    $match = @{}
    if ($Mac) { Add-IseDcTerm $filter $match 'MAC_ADDRESS' $Mac }

    $arguments = @{
        View = 'endpoints_data'
        Column = @('MAC_ADDRESS', 'PROBE_DATA')
    }
    if ($filter.Count) { $arguments['Filter'] = $filter }
    if ($match.Count) { $arguments['Match'] = $match }
    foreach ($parameter in @('First', 'Wait', 'Force')) {
        if ($PSBoundParameters.ContainsKey($parameter)) {
            $arguments[$parameter] = $PSBoundParameters[$parameter]
        }
    }

    foreach ($row in @(Invoke-IseDcQuery @arguments)) {
        if ($null -eq $row) { continue }
        $probe = $row.probe_data
        if ($null -eq $probe) { continue }
        if ($probe.truncated) {
            # A warning rather than a note on each row: it is one fact about
            # the endpoint, and repeating it per attribute would bury it.
            Write-Warning ("$($row.mac_address): $($probe.note)")
        }
        if (-not $probe.attributes) { continue }

        $pairs = @($probe.attributes.PSObject.Properties |
            Where-Object { $IncludeEmpty -or -not [string]::IsNullOrEmpty($_.Value) } |
            Where-Object { -not $Name -or $_.Name -like $Name } |
            Sort-Object Name)

        if ($AsObject) {
            $flat = [ordered]@{ mac_address = $row.mac_address }
            foreach ($pair in $pairs) { $flat[$pair.Name] = $pair.Value }
            $object = [pscustomobject]$flat
            $object.PSObject.TypeNames.Insert(0, 'Ise.Dc.ProbeObject')
            $object
            continue
        }
        foreach ($pair in $pairs) {
            $attribute = [pscustomobject]@{
                PSTypeName = 'Ise.Dc.ProbeAttribute'
                Mac        = $row.mac_address
                Name       = $pair.Name
                Value      = $pair.Value
            }
            $attribute
        }
    }
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
    'Get-IseCliReadme',
    'Get-IseApiRoot', 'Set-IseApiRoot', 'Invoke-IseApi',
    'Get-IseHealth', 'Get-IseDataset', 'Get-IseProvider',
    'Get-IseTarget', 'Get-IsePlan', 'Get-IseDegraded',
    'Get-IseDcView', 'Get-IseDcColumn', 'Get-IseDcStatus', 'Invoke-IseDcQuery',
    'Get-IseDcRadiusAuth', 'Get-IseDcRadiusAccounting', 'Get-IseDcRadiusError',
    'Get-IseDcEndpoint', 'Get-IseDcTacacsAuth', 'Get-IseDcTacacsCommand',
    'Get-IseDcTacacsAuthorization', 'Get-IseDcPosture', 'Get-IseDcNodeHealth',
    'Get-IseDcNodePerformance',
    'Get-IseRadiusLiveLog', 'Get-IseContextVisibility',
    'Get-IseEndpointProbe'
)
