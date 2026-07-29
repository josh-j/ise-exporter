#!/usr/bin/env pwsh
# Contract test for the ise-cli3 PowerShell module.
#
# The module is a thin client for routes the exporter serves, so the thing worth
# testing is the wire: does an operator's parameter become the query string the
# server documents, and does the server's refusal become a sentence. Both halves
# of docs/ise-cli3-plan.md are built against that contract independently, so a
# stub that answers exactly what the contract says is a real check on this half
# rather than a mirror of it -- the recorded URLs below are what the Python
# route handlers must accept.
#
# Self-contained: it starts an HttpListener on a free loopback port, points the
# module at it, and exits non-zero on any failure, so CI can run it behind
# `command -v pwsh`.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$script:Passed = 0
$script:Failed = 0

function Assert-That {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')
    if ($Condition) {
        $script:Passed++
        Write-Host "  ok   $Name" -ForegroundColor Green
    }
    else {
        $script:Failed++
        Write-Host "  FAIL $Name" -ForegroundColor Red
        if ($Detail) { Write-Host "       $Detail" -ForegroundColor DarkRed }
    }
}

function Assert-Equal {
    param([string]$Name, $Expected, $Actual)
    Assert-That $Name ("$Expected" -eq "$Actual") "expected: $Expected`n       actual:   $Actual"
}

function Assert-Like {
    param([string]$Name, [string]$Pattern, [string]$Actual)
    Assert-That $Name ($Actual -like $Pattern) "pattern: $Pattern`n       actual:  $Actual"
}

# --- the stub exporter -------------------------------------------------------

# Canned contract JSON. The catalogue deliberately spells the RADIUS accounting
# user column USER_NAME and the authentication one USERNAME: ISE is not
# consistent across views, and the typed cmdlets are supposed to resolve the
# spelling against the descriptor rather than hard-code one and hope.
$views = @(
    @{
        name = 'radius_authentications'; view = 'RADIUS_AUTHENTICATIONS'
        description = 'one row per RADIUS authentication event'
        time_column = 'TIMESTAMP'; time_kind = 'timestamp'
        default_columns = @('TIMESTAMP', 'USERNAME', 'CALLING_STATION_ID', 'DEVICE_NAME', 'FAILED')
        columns = @('AUTHENTICATION_METHOD', 'AUTHORIZATION_PROFILES', 'CALLING_STATION_ID',
                    'DEVICE_NAME', 'ENDPOINT_PROFILE', 'FAILED', 'FAILURE_REASON', 'ISE_NODE',
                    'POSTURE_STATUS', 'RESPONSE_TIME', 'TIMESTAMP', 'USERNAME')
        available = $true
    },
    @{
        name = 'radius_accounting'; view = 'RADIUS_ACCOUNTING'
        description = 'RADIUS accounting starts and stops'
        time_column = 'TIMESTAMP'; time_kind = 'timestamp'
        default_columns = @('TIMESTAMP', 'USER_NAME', 'DEVICE_NAME', 'ACCT_STATUS_TYPE')
        columns = @('ACCT_SESSION_TIME', 'ACCT_STATUS_TYPE', 'CALLING_STATION_ID',
                    'DEVICE_NAME', 'ISE_NODE', 'TIMESTAMP', 'USER_NAME')
        available = $true
    },
    @{
        name = 'endpoints_data'; view = 'ENDPOINTS_DATA'
        description = 'the endpoint database, current state'
        time_column = $null; time_kind = $null
        default_columns = @('MAC_ADDRESS', 'ENDPOINT_POLICY', 'UPDATE_TIME')
        columns = @('ENDPOINT_IP', 'ENDPOINT_POLICY', 'HOSTNAME', 'IDENTITY_GROUP_ID',
                    'MAC_ADDRESS', 'PORTAL_USER', 'POSTURE_APPLICABLE', 'UPDATE_TIME')
        available = $true
    },
    @{
        name = 'tacacs_accounting_last_two_days'; view = 'TACACS_ACCOUNTING_LAST_TWO_DAYS'
        description = 'device-administration commands, 48 h retention'
        time_column = 'EPOCH_TIME'; time_kind = 'epoch'
        default_columns = @('EPOCH_TIME', 'USERNAME', 'DEVICE_NAME', 'COMMAND', 'COMMAND_ARGS')
        columns = @('COMMAND', 'COMMAND_ARGS', 'DEVICE_NAME', 'EPOCH_TIME', 'USERNAME')
        available = $true
    },
    @{
        name = 'posture_assessment_by_condition'; view = 'POSTURE_ASSESSMENT_BY_CONDITION'
        description = 'per-condition posture results'
        time_column = 'LOGGED_AT'; time_kind = 'timestamp'
        default_columns = @(); columns = @(); available = $false
    }
)

$status = @{
    configured = $true; schema_discovered = $true
    views_total = 5; views_available = 4
    duty_cycle_percent = 5.0; cooldown_remaining_seconds = 12.3; busy = $false
    last_query = @{ view = 'radius_authentications'; rows = 100
                    elapsed_seconds = 1.9; at_age_seconds = 42.0; result = 'success' }
}

$rows = @{
    radius_authentications = @(
        @{ timestamp = '2026-07-27 19:48:01'; username = 'jdoe'
           calling_station_id = 'AA:BB:CC:11:22:33'; device_name = 'core-1'
           failed = 1; failure_reason = '22056 Subject not found'
           ise_node = 'laba-psn-01'; endpoint_profile = 'Cisco-IP-Phone'
           authorization_profiles = $null; authentication_method = 'dot1x'
           posture_status = $null },
        @{ timestamp = '2026-07-27 19:47:55'; username = 'asmith'
           calling_station_id = 'AA:BB:CC:44:55:66'; device_name = 'core-2'
           failed = 0; failure_reason = $null
           ise_node = 'laba-psn-02'; endpoint_profile = 'Workstation'
           authorization_profiles = 'PermitAccess'; authentication_method = 'dot1x'
           posture_status = 'Compliant' }
    )
    radius_accounting = @(
        @{ timestamp = '2026-07-27 19:40:00'; user_name = 'jdoe'
           device_name = 'core-1'; acct_status_type = 'Start' }
    )
    endpoints_data = @(
        # probe_data is the decoded shape the exporter sends: the attributes it
        # could prove, plus what the header said was there. The declared count
        # exceeds the parsed one because Cisco's view exposes only the first
        # 2000 bytes of the profiling buffer.
        @{ mac_address = 'AA:BB:CC:11:22:33'; endpoint_policy = 'Cisco-IP-Phone'
           update_time = '2026-07-26 07:19:00'; endpoint_ip = '10.10.1.51'
           hostname = 'phone-51'
           probe_data = @{
               encoding = 'ise-tlv'; count = 3; declared = 137; truncated = $true
               note = ('ISE serialised 137 attributes; this view exposes the first ' +
                       '2000 bytes of the profiling buffer, which held 3. The other ' +
                       '134 are in ISE but not reachable through Data Connect')
               attributes = @{
                   OUI = 'Cisco Systems, Inc'
                   NetworkDeviceName = 'campus-corp-wired'
                   assetHwRevision = ''
               }
           } },
        # Dotted-quad spelling on purpose: the same endpoint ISE writes as
        # AA:BB:CC:44:55:66 in CALLING_STATION_ID, so the join has to normalise.
        @{ mac_address = 'AABB.CC44.5566'; endpoint_policy = 'Workstation'
           update_time = '2026-07-26 07:20:00'; endpoint_ip = '10.10.1.52'
           hostname = 'ws-52' },
        # Nothing authenticated this one inside the window.
        @{ mac_address = 'AA:BB:CC:99:99:99'; endpoint_policy = 'Unknown'
           update_time = '2026-07-20 01:00:00'; endpoint_ip = $null
           hostname = $null }
    )
}

# What /api/v1/pxgrid/sessions serves: the exporter's projection of the live
# session directory, already reduced to the fields the operator surface shows.
$sessions = @{
    row_count = 1; matched = 1; truncated = $false; snapshot_age_seconds = 12.0
    sessions = @(
        @{ mac_address = 'AA:BB:CC:11:22:33'; ip_address = '10.200.40.144'
           user_name = 'jdoe'; nad = 'campus-corp-wired'; nas_ip_address = '10.200.30.1'
           nas_port = 'GigabitEthernet1/0/1'; endpoint_profile = 'Cisco-IP-Phone'
           posture_status = 'Compliant'; authorization_profiles = 'PermitAccess'
           security_group = 'Employees'; ise_node = 'laba-psn-01'
           auth_method = 'dot1x'; auth_protocol = 'PAP_ASCII'
           session_state = 'STARTED'; audit_session_id = '0a0a0a0a00000001'
           last_update = '2026-07-29 16:40:00' }
    )
}

$listenerState = [hashtable]::Synchronized(@{
    Requests   = [System.Collections.ArrayList]::Synchronized([System.Collections.ArrayList]::new())
    FlakyCalls = 0
    Ready      = $false
    Error      = $null
})

# A free port, released immediately: HttpListener wants a prefix, not a socket,
# and racing for one is cheaper than picking a number and hoping.
$probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$probe.Start()
$port = $probe.LocalEndpoint.Port
$probe.Stop()
$root = "http://127.0.0.1:$port"

$serverScript = {
    function Write-Json {
        param($Context, [int]$Status, $Payload)
        $body = [System.Text.Encoding]::UTF8.GetBytes(($Payload | ConvertTo-Json -Depth 8 -Compress))
        $Context.Response.StatusCode = $Status
        $Context.Response.ContentType = 'application/json'
        $Context.Response.ContentLength64 = $body.Length
        $Context.Response.OutputStream.Write($body, 0, $body.Length)
        $Context.Response.Close()
    }

    try {
        $listener = [System.Net.HttpListener]::new()
        $listener.Prefixes.Add("$root/")
        $listener.Start()
        $state['Ready'] = $true

        $running = $true
        while ($running -and $listener.IsListening) {
            $context = $listener.GetContext()
            $request = $context.Request
            # RawUrl is the request line verbatim: the assertions are about what
            # the client put on the wire, not what .NET would normalise it to.
            [void]$state.Requests.Add($request.RawUrl)
            $path = $request.Url.AbsolutePath
            $view = $request.QueryString['view']

            # One response per request, and one branch per response: an unanswered
            # or twice-answered request would hang the client for its 90 s query
            # timeout rather than failing an assertion.
            if ($path -eq '/shutdown') {
                $running = $false
                Write-Json $context 200 @{ ok = $true }
            }
            elseif ($path -eq '/api/v1/dataconnect/views') {
                Write-Json $context 200 $views
            }
            elseif ($path -eq '/api/v1/dataconnect/status') {
                Write-Json $context 200 $status
            }
            elseif ($path -eq '/api/v1/mnt/session') {
                $mac = $request.QueryString['mac']
                if ($mac -eq '02:1E:5E:99:99:99') {
                    Write-Json $context 200 @{ mac_address = $mac; found = $false
                                               session = $null }
                }
                else {
                    Write-Json $context 200 @{
                        mac_address = $mac; found = $true
                        session = @{
                            user_name = 'ise-exporter-test-user'
                            acct_input_octets = '84213'
                            acct_output_octets = '12904'
                            audit_session_id = '0B0B0B0B00000001'
                            selected_azn_profiles = 'Lab_Employee_Full'
                            execution_steps = '11001,11017,11507'
                            empty_on_purpose = ''
                        }
                    }
                }
            }
            elseif ($path -eq '/api/v1/pxgrid/sessions') {
                if ($request.QueryString['mac'] -eq 'refuse') {
                    Write-Json $context 409 @{
                        error = 'pxgrid_unconfigured'
                        detail = 'this exporter has no pxGrid target configured'
                    }
                }
                else { Write-Json $context 200 $sessions }
            }
            elseif ($path -eq '/api/v1/dataconnect/query') {
                $refusal = switch ($view) {
                    'busy_view' {
                        @{ code = 429; body = @{
                            error = 'busy'; detail = 'another explorer query is in flight' } }
                    }
                    'cooldown_view' {
                        @{ code = 503; body = @{ error = 'cooldown'; retry_after_seconds = 38 } }
                    }
                    'pending_view' {
                        @{ code = 503; body = @{
                            error = 'schema_pending'
                            detail = 'catalog discovery has not completed' } }
                    }
                    'nosuch_view' {
                        @{ code = 404; body = @{ error = 'unknown_view'; detail = 'nosuch_view' } }
                    }
                    'flaky_view' {
                        $state['FlakyCalls'] = [int]$state['FlakyCalls'] + 1
                        if ([int]$state['FlakyCalls'] -eq 1) {
                            @{ code = 503; body = @{ error = 'cooldown'; retry_after_seconds = 1 } }
                        }
                    }
                }
                if ($refusal) {
                    Write-Json $context $refusal.code $refusal.body
                }
                else {
                    $payload = [ordered]@{
                        view = $view
                        sql = "SELECT * FROM $view FETCH FIRST :limit ROWS ONLY"
                        binds = @{ limit = 100 }
                    }
                    if ($request.QueryString['explain'] -eq '1') {
                        $payload['rows'] = $null
                        $payload['row_count'] = $null
                        $payload['truncated'] = $null
                        $payload['elapsed_seconds'] = $null
                        $payload['cooldown_seconds'] = $null
                    }
                    else {
                        $result = @($rows[$view])
                        if (-not $result -or $null -eq $result[0]) { $result = @(@{ view = $view }) }
                        $first = $request.QueryString['first']
                        $payload['rows'] = $result
                        $payload['row_count'] = $result.Count
                        $payload['truncated'] = ($first -and [int]$first -le $result.Count)
                        $payload['elapsed_seconds'] = 1.9
                        $payload['cooldown_seconds'] = 36.4
                    }
                    Write-Json $context 200 $payload
                }
            }
            else {
                Write-Json $context 404 @{ error = 'not_found'; detail = $path }
            }
        }
        $listener.Stop()
        $listener.Close()
    }
    catch {
        $state['Error'] = $_.ToString()
        $state['Ready'] = $true
    }
}

$runspace = [runspacefactory]::CreateRunspace()
$runspace.Open()
foreach ($pair in @{ state = $listenerState; root = $root; views = $views;
                     status = $status; rows = $rows; sessions = $sessions }.GetEnumerator()) {
    $runspace.SessionStateProxy.SetVariable($pair.Key, $pair.Value)
}
$server = [powershell]::Create()
$server.Runspace = $runspace
[void]$server.AddScript($serverScript)
$serverHandle = $server.BeginInvoke()

$deadline = (Get-Date).AddSeconds(10)
while (-not $listenerState['Ready'] -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 50 }
if ($listenerState['Error']) { throw "stub listener failed to start: $($listenerState['Error'])" }
if (-not $listenerState['Ready']) { throw 'stub listener did not start within 10s' }

# --- the tests ---------------------------------------------------------------

$manifestPath = Join-Path $PSScriptRoot '../powershell/Ise.Cli3/Ise.Cli3.psd1'
$manifestPath = (Resolve-Path $manifestPath).Path

try {
    Write-Host ''
    Write-Host "stub exporter on $root" -ForegroundColor DarkGray
    Write-Host ''
    Write-Host 'module' -ForegroundColor Cyan

    # The env var is the documented way to point a shell at another exporter, so
    # it is what the test uses to reach the stub; Set-IseApiRoot is exercised too.
    $env:ISE_EXPORTER_API = $root
    Import-Module $manifestPath -Force
    $module = Get-Module Ise.Cli3
    Assert-That 'manifest imports' ($null -ne $module)
    Assert-Equal 'ModuleVersion is 3.2.0' '3.2.0' $module.Version

    $manifest = Import-PowerShellDataFile $manifestPath
    $declared = @($manifest.FunctionsToExport | Sort-Object)
    $exported = @($module.ExportedFunctions.Keys | Sort-Object)
    Assert-Equal 'exports match the manifest' ($declared -join ',') ($exported -join ',')
    Assert-That 'every Dc cmdlet the plan lists is exported' (
        @('Get-IseDcView', 'Get-IseDcColumn', 'Get-IseDcStatus', 'Invoke-IseDcQuery',
          'Get-IseDcRadiusAuth', 'Get-IseDcRadiusAccounting', 'Get-IseDcRadiusError',
          'Get-IseDcEndpoint', 'Get-IseDcTacacsAuth', 'Get-IseDcTacacsCommand',
          'Get-IseDcTacacsAuthorization', 'Get-IseDcPosture', 'Get-IseDcNodeHealth',
          'Get-IseDcNodePerformance' | Where-Object { $exported -notcontains $_ }).Count -eq 0)
    Assert-Equal 'Set-IseApiRoot points the session at the stub' $root (Set-IseApiRoot -Uri $root).ApiRoot

    # The guide ships inside the module because docs/ is neither installed nor
    # committed; a shipped file is the only one an operator on the appliance
    # host has. Asserting it is present is asserting the install is complete.
    $readme = Get-IseCliReadme
    Assert-That 'the guide ships with the module' ($readme.Length -gt 1000)
    Assert-Like 'and says what costs Oracle time' '*duty cycle*' $readme
    $sections = @(Get-IseCliReadme -List)
    Assert-That 'the guide names its sections' ($sections.Count -ge 5)
    Assert-That 'and every one is a heading, not a body line' (
        @($sections | Where-Object { $_ -match '^#' }).Count -eq 0)
    $one = Get-IseCliReadme -Section 'refused'
    Assert-Like '-Section prints the section asked for' '*schema_pending*' $one
    Assert-That '-Section prints only that section' ($one.Length -lt $readme.Length)
    Assert-That 'a section stops at the next heading' (
        @((Get-IseCliReadme -Section 'refused' -Raw) -split "`n" |
            Where-Object { $_ -match '^##\s' }).Count -eq 1)

    # Show-Markdown was tried and rejected: it collapses every table onto one
    # line, emits cursor escapes inside fenced code, and never wraps a
    # paragraph. These are the four things it got wrong.
    $rendered = Get-IseCliReadme -Width 78
    $plain = ($rendered -replace "$([char]27)\[[0-9;]*m", '') -split "`n"
    Assert-That 'rendered output carries no markdown headings' (
        @($plain | Where-Object { $_ -match '^#' }).Count -eq 0)
    # A markdown table row starts with a pipe; a PowerShell pipeline inside a
    # fenced block has one in the middle and must survive untouched.
    Assert-That 'and no markdown table rows' (
        @($plain | Where-Object { $_ -match '^\s*\|' }).Count -eq 0)
    Assert-Like 'while pipelines in examples keep their pipe' `
        '*| Group-Object failure_reason*' $rendered
    Assert-That 'and no leftover backticks or bold markers' (
        @($plain | Where-Object { $_ -match '``' -or $_ -match '\*\*' }).Count -eq 0)
    # Prose wraps; a command does not, because a wrapped command is a broken
    # one. Code carries its own colour, so "only code may overflow" is a rule
    # the output can actually be measured against rather than guessed at.
    $code = "$([char]27)[38;5;150m"
    $over = @($rendered -split "`n" | Where-Object {
        (($_ -replace "$([char]27)\[[0-9;]*m", '').Length) -gt 78 })
    Assert-That 'nothing but code exceeds the width' (
        @($over | Where-Object { -not $_.StartsWith("  $code") }).Count -eq 0)
    Assert-That 'and the prose really did wrap' (
        @($plain | Where-Object {
            $_ -match 'duty cycle' -and $_.Length -le 78 }).Count -gt 0)
    Assert-Like 'while keeping the table readable' '*cooling down*' $rendered
    Assert-Like 'and the commands intact' '*Get-IseRadiusLiveLog -Last 1h*' $rendered

    $raw = Get-IseCliReadme -Raw
    Assert-Like '-Raw is still the markdown source' '*## What costs what*' $raw
    Assert-That '-Raw carries no ANSI' (
        $raw -notmatch "$([char]27)\[")
    $threw = $false
    try { Get-IseCliReadme -Section 'no-such-section' } catch { $threw = $true }
    Assert-That 'an unknown section says so instead of printing nothing' $threw
    # No exporter is involved: the guide is a file, and reading it must not
    # cost a round trip, let alone an Oracle statement.
    $before = $listenerState.Requests.Count
    $null = Get-IseCliReadme
    Assert-Equal 'reading the guide talks to nothing' $before $listenerState.Requests.Count

    Write-Host ''
    Write-Host 'discovery' -ForegroundColor Cyan

    $all = @(Get-IseDcView)
    Assert-Equal 'Get-IseDcView returns every descriptor' 5 $all.Count
    Assert-Equal 'descriptors are typed' 'Ise.Dc.View' $all[0].PSObject.TypeNames[0]
    Assert-Equal 'descriptors carry their row type' 'Ise.Dc.RadiusAuthentications' $all[0].TypeName
    Assert-Equal 'Get-IseDcView filters on wildcards' 2 @(Get-IseDcView radius*).Count
    Assert-Equal 'an unavailable view still lists' $false @(Get-IseDcView posture*)[0].available

    $before = $listenerState.Requests.Count
    $null = Get-IseDcView
    Assert-Equal 'descriptors are cached for the session' $before $listenerState.Requests.Count
    $null = Get-IseDcView -Refresh
    Assert-Equal '-Refresh re-fetches' ($before + 1) $listenerState.Requests.Count

    $columns = @(Get-IseDcColumn -View radius_authentications)
    Assert-Equal 'Get-IseDcColumn lists the catalogue' 12 $columns.Count
    Assert-Equal 'columns are typed' 'Ise.Dc.Column' $columns[0].PSObject.TypeNames[0]
    Assert-That 'columns say which are default' (
        (@($columns | Where-Object { $_.Default }).Count) -eq 5)
    Assert-Equal 'Get-IseDcColumn filters on wildcards' 1 @(Get-IseDcColumn radius_authentications *station*).Count

    $dcStatus = Get-IseDcStatus
    Assert-Equal 'Get-IseDcStatus is typed' 'Ise.Dc.Status' $dcStatus.PSObject.TypeNames[0]
    Assert-Equal 'Get-IseDcStatus carries the cooldown' 12.3 $dcStatus.cooldown_remaining_seconds

    $line = 'Invoke-IseDcQuery -View radius'
    $completion = (TabExpansion2 -inputScript $line -cursorColumn $line.Length).CompletionMatches
    Assert-Equal '-View completes from the cache' 2 @($completion).Count

    Write-Host ''
    Write-Host 'the generic verb' -ForegroundColor Cyan

    $result = @(Invoke-IseDcQuery -View radius_authentications -Last 2h)
    Assert-Equal 'rows come back' 2 $result.Count
    Assert-Equal 'rows carry the view PSTypeName' 'Ise.Dc.RadiusAuthentications' $result[0].PSObject.TypeNames[0]
    Assert-Equal 'rows keep their columns' 'jdoe' $result[0].username
    Assert-Equal 'the window travels as last=' '/api/v1/dataconnect/query?last=2h&view=radius_authentications' $listenerState.Requests[-1]

    $null = Invoke-IseDcQuery -View radius_authentications `
        -Filter @{ ISE_NODE = 'laba-ise-001'; DEVICE_NAME = 'core-1' } `
        -Match @{ USERNAME = 'admin*' } -Column TIMESTAMP, USERNAME `
        -OrderBy TIMESTAMP -Descending -First 25
    Assert-Equal 'every parameter maps onto the documented query string' (
        '/api/v1/dataconnect/query?cols=TIMESTAMP%2CUSERNAME&desc=1' +
        '&eq=DEVICE_NAME%3Acore-1&eq=ISE_NODE%3Alaba-ise-001&first=25' +
        '&like=USERNAME%3Aadmin%2A&order=TIMESTAMP&view=radius_authentications'
    ) $listenerState.Requests[-1]

    $null = Invoke-IseDcQuery -View radius_authentications -OrderBy TIMESTAMP
    Assert-Like '-OrderBy without -Descending asks for ascending' '*desc=0*' $listenerState.Requests[-1]

    $null = Invoke-IseDcQuery -View radius_authentications -Last 2h -Force
    Assert-Like '-Force travels as force=1' '*force=1*' $listenerState.Requests[-1]

    # 'all' is a value of -Last, not another parameter: rows become the only
    # bound and the server reads newest-first.
    $null = Invoke-IseDcQuery -View radius_authentications -Last all -First 50
    Assert-Like '-Last all travels as last=all' '*first=50*last=all*' $listenerState.Requests[-1]

    $null = Invoke-IseDcQuery -View radius_authentications -Last 1h `
        -Min @{ RESPONSE_TIME = 500 } -Max @{ RESPONSE_TIME = 2000 } `
        -Exclude @{ USERNAME = 'svc_probe' } -IsNull FAILURE_REASON -NotNull DEVICE_NAME
    Assert-Equal 'ranges, exclusion and null tests map onto the wire' (
        '/api/v1/dataconnect/query?ge=RESPONSE_TIME%3A500&last=1h' +
        '&le=RESPONSE_TIME%3A2000&ne=USERNAME%3Asvc_probe' +
        '&notnull=DEVICE_NAME&null=FAILURE_REASON&view=radius_authentications'
    ) $listenerState.Requests[-1]

    $grouped = @(Invoke-IseDcQuery -View radius_authentications -Last 1d `
        -GroupBy DEVICE_NAME, ISE_NODE -Aggregate avg:RESPONSE_TIME)
    Assert-Equal 'grouping and aggregates travel in projection order' (
        '/api/v1/dataconnect/query?agg=avg%3ARESPONSE_TIME' +
        '&group=DEVICE_NAME&group=ISE_NODE&last=1d&view=radius_authentications'
    ) $listenerState.Requests[-1]
    Assert-Equal 'grouped rows are typed as grouped, not as the view' `
        'Ise.Dc.Grouped' $grouped[0].PSObject.TypeNames[0]

    # -All is a display choice, not a query change: the exporter already sent
    # every column, so the switch must not reach the wire.
    $wide = @(Invoke-IseDcQuery -View radius_authentications -Last 2h -All)
    Assert-Equal '-All withholds the curated type so no format table narrows it' `
        'Ise.Dc.Row' $wide[0].PSObject.TypeNames[0]
    Assert-Equal '-All still returns the rows' 2 $wide.Count
    Assert-Equal '-All keeps every column on the object' 'jdoe' $wide[0].username
    Assert-Equal '-All does not travel to the server' `
        '/api/v1/dataconnect/query?last=2h&view=radius_authentications' `
        $listenerState.Requests[-1]

    $typedWide = @(Get-IseDcRadiusAuth -Last 2h -All)
    Assert-Equal '-All reaches the typed cmdlets too' `
        'Ise.Dc.Row' $typedWide[0].PSObject.TypeNames[0]

    $sql = Invoke-IseDcQuery -View radius_authentications -Last 1d -AsSql
    Assert-Like '-AsSql hits explain=1' '*explain=1*' $listenerState.Requests[-1]
    Assert-Equal '-AsSql is typed' 'Ise.Dc.Sql' $sql.PSObject.TypeNames[0]
    Assert-Like '-AsSql returns the statement' '*FETCH FIRST :limit ROWS ONLY' $sql.Sql
    Assert-Equal '-AsSql returns binds, not rows' 100 $sql.Binds.limit

    $warnings = @()
    $null = Invoke-IseDcQuery -View radius_authentications -First 2 `
        -WarningVariable warnings -WarningAction SilentlyContinue
    Assert-Like 'truncation warns and names -First' '*-First*' ($warnings -join ' ')

    Write-Host ''
    Write-Host 'typed cmdlets' -ForegroundColor Cyan

    $null = Get-IseDcRadiusAuth -User jdoe -Mac 'AA:BB:*' -Nad core-1 -Failed -Last 1h
    Assert-Equal 'Get-IseDcRadiusAuth maps its filters' (
        '/api/v1/dataconnect/query?eq=DEVICE_NAME%3Acore-1&eq=FAILED%3A1' +
        '&eq=USERNAME%3Ajdoe&last=1h&like=CALLING_STATION_ID%3AAA%3ABB%3A%2A' +
        '&view=radius_authentications'
    ) $listenerState.Requests[-1]

    $null = Get-IseDcRadiusAuth -Passed -First 5
    Assert-Equal '-Passed is the same flag, inverted' (
        '/api/v1/dataconnect/query?eq=FAILED%3A0&first=5&view=radius_authentications'
    ) $listenerState.Requests[-1]

    $null = Get-IseDcRadiusAuth -Failed -Last 1h -Force
    Assert-Like 'the typed cmdlets pass -Force through' '*force=1*' $listenerState.Requests[-1]

    $failure = try { Get-IseDcRadiusAuth -Failed -Passed; '' } catch { $_.Exception.Message }
    Assert-Like '-Failed and -Passed together are refused' '*not both*' $failure

    # The stub's accounting catalogue spells it USER_NAME; the cmdlet must follow
    # the catalogue rather than the spelling its sibling view uses.
    $null = Get-IseDcRadiusAccounting -User jdoe -Last 1d
    Assert-Equal 'the user column comes from the catalogue, not a guess' (
        '/api/v1/dataconnect/query?eq=USER_NAME%3Ajdoe&last=1d&view=radius_accounting'
    ) $listenerState.Requests[-1]

    $null = Get-IseDcRadiusError -Nad 'core-*' -Last 4h
    Assert-Equal 'Get-IseDcRadiusError uses NETWORK_DEVICE_NAME' (
        '/api/v1/dataconnect/query?last=4h&like=NETWORK_DEVICE_NAME%3Acore-%2A' +
        '&view=radius_errors_view'
    ) $listenerState.Requests[-1]

    $endpoints = @(Get-IseDcEndpoint -Policy 'Cisco-IP-Phone*' -First 500)
    Assert-Equal 'Get-IseDcEndpoint maps -Policy' (
        '/api/v1/dataconnect/query?first=500&like=ENDPOINT_POLICY%3ACisco-IP-Phone%2A' +
        '&view=endpoints_data'
    ) $listenerState.Requests[-1]
    Assert-Equal 'endpoint rows are typed' 'Ise.Dc.EndpointsData' $endpoints[0].PSObject.TypeNames[0]

    # -Last is an optional filter here, not the always-on bound the event views
    # get: the server windows on UPDATE_TIME only when asked.
    $null = Get-IseDcEndpoint -Policy 'Cisco-IP-Phone*' -Last 1h
    Assert-Like 'Get-IseDcEndpoint passes -Last through' '*last=1h*' $listenerState.Requests[-1]

    $null = Get-IseDcTacacsAuth -User jdoe -Nad 'sw-*' -Failed -Last 1d
    Assert-Equal 'Get-IseDcTacacsAuth maps -Failed onto STATUS' (
        '/api/v1/dataconnect/query?eq=USERNAME%3Ajdoe&last=1d&like=DEVICE_NAME%3Asw-%2A' +
        '&like=STATUS%3AFail%2A&view=tacacs_authentication_last_two_days'
    ) $listenerState.Requests[-1]

    $null = Get-IseDcTacacsCommand -User jdoe -Command 'conf*' -Last 1d
    Assert-Equal 'Get-IseDcTacacsCommand maps -Command' (
        '/api/v1/dataconnect/query?eq=USERNAME%3Ajdoe&last=1d&like=COMMAND%3Aconf%2A' +
        '&view=tacacs_accounting_last_two_days'
    ) $listenerState.Requests[-1]

    $null = Get-IseDcTacacsAuthorization -User jdoe
    Assert-Equal 'Get-IseDcTacacsAuthorization maps -User' (
        '/api/v1/dataconnect/query?eq=USERNAME%3Ajdoe&view=tacacs_authorization_last_two_days'
    ) $listenerState.Requests[-1]

    $null = Get-IseDcPosture -Mac 'AA:*' -Status NonCompliant -Last 1d
    Assert-Equal 'Get-IseDcPosture maps -Mac and -Status' (
        '/api/v1/dataconnect/query?eq=POSTURE_STATUS%3ANonCompliant&last=1d' +
        '&like=ENDPOINT_MAC_ADDRESS%3AAA%3A%2A&view=posture_assessment_by_endpoint'
    ) $listenerState.Requests[-1]

    $null = Get-IseDcNodeHealth -Node laba-ise-002 -Last 1h
    Assert-Equal 'Get-IseDcNodeHealth maps -Node' (
        '/api/v1/dataconnect/query?eq=ISE_NODE%3Alaba-ise-002&last=1h&view=system_summary'
    ) $listenerState.Requests[-1]

    $null = Get-IseDcNodePerformance -Node 'laba-*' -Column ISE_NODE, AVG_TPS
    Assert-Equal 'Get-IseDcNodePerformance passes -Column through' (
        '/api/v1/dataconnect/query?cols=ISE_NODE%2CAVG_TPS&like=ISE_NODE%3Alaba-%2A' +
        '&view=key_performance_metrics'
    ) $listenerState.Requests[-1]

    Write-Host ''
    Write-Host 'screen replicas' -ForegroundColor Cyan

    $live = @(Get-IseRadiusLiveLog -Last 1h)
    Assert-Equal 'live logs order newest-first server-side' (
        '/api/v1/dataconnect/query?desc=1&last=1h&order=TIMESTAMP' +
        '&view=radius_authentications'
    ) $listenerState.Requests[-1]
    Assert-Equal 'live log rows are typed for the live-log table' `
        'Ise.Dc.RadiusLiveLog' $live[0].PSObject.TypeNames[0]
    Assert-Equal 'the FAILED flag becomes the screen word' 'Fail' $live[0].status
    Assert-Equal 'and a pass says so' 'Pass' $live[1].status
    Assert-Equal 'the underlying flag survives for anything that wants a number' `
        1 $live[0].failed

    $null = Get-IseRadiusLiveLog -Status Fail -Last 2h
    Assert-Like '-Status Fail binds FAILED=1' '*eq=FAILED%3A1*' $listenerState.Requests[-1]
    $null = Get-IseRadiusLiveLog -Status Pass -Last 2h
    Assert-Like '-Status Pass binds FAILED=0' '*eq=FAILED%3A0*' $listenerState.Requests[-1]

    $null = Get-IseRadiusLiveLog -Identity 'jdoe' -Endpoint 'AA:BB:*' -Nad 'core-*' `
        -Node 'laba-psn-01' -Last 30m
    Assert-Equal 'the live-log filters map onto the documented query string' (
        '/api/v1/dataconnect/query?desc=1&eq=ISE_NODE%3Alaba-psn-01&eq=USERNAME%3Ajdoe' +
        '&last=30m&like=CALLING_STATION_ID%3AAA%3ABB%3A%2A&like=DEVICE_NAME%3Acore-%2A' +
        '&order=TIMESTAMP&view=radius_authentications'
    ) $listenerState.Requests[-1]

    # A failure reason cannot appear on a pass, so asking for one is asking for
    # failures; saying that server-side is cheaper than filtering the rows back.
    $null = Get-IseRadiusLiveLog -FailureReason '22056*' -Last 1h
    Assert-Like '-FailureReason implies failures' '*eq=FAILED%3A1*' $listenerState.Requests[-1]

    $wideLive = @(Get-IseRadiusLiveLog -Last 1h -All)
    Assert-Equal '-All still declines the replica table' `
        'Ise.Dc.Row' $wideLive[0].PSObject.TypeNames[0]
    Assert-Equal '-All keeps the derived status' 'Fail' $wideLive[0].status

    $context = @(Get-IseContextVisibility)
    Assert-Equal 'context visibility reads the endpoint database' (
        '/api/v1/dataconnect/query?view=endpoints_data') $listenerState.Requests[-1]
    Assert-Equal 'context rows are typed for the context table' `
        'Ise.Dc.ContextVisibility' $context[0].PSObject.TypeNames[0]
    Assert-Equal 'context visibility returns every endpoint' 3 $context.Count
    Assert-That 'without -WithLastAuth there is no identity to show' (
        $null -eq $context[0].auth_identity)

    $null = Get-IseContextVisibility -Mac 'AA:BB:*' -Ip '10.10.1.*' -Hostname 'phone-*' `
        -Profile 'Cisco-IP-Phone*' -User 'jdoe'
    # -User jdoe carries no wildcard, so it is an equality like any other exact
    # value; only the four patterns become LIKE.
    Assert-Equal 'the context filters map onto the documented query string' (
        '/api/v1/dataconnect/query?eq=PORTAL_USER%3Ajdoe' +
        '&like=ENDPOINT_IP%3A10.10.1.%2A&like=ENDPOINT_POLICY%3ACisco-IP-Phone%2A' +
        '&like=HOSTNAME%3Aphone-%2A&like=MAC_ADDRESS%3AAA%3ABB%3A%2A&view=endpoints_data'
    ) $listenerState.Requests[-1]

    $before = $listenerState.Requests.Count
    $joined = @(Get-IseContextVisibility -WithLastAuth -AuthLast 4h)
    Assert-Equal '-WithLastAuth costs exactly one extra statement' 2 (
        $listenerState.Requests.Count - $before)
    Assert-Equal 'the authentication lookup is newest-first over its own window' (
        '/api/v1/dataconnect/query?desc=1&last=4h&order=TIMESTAMP' +
        '&view=radius_authentications'
    ) $listenerState.Requests[-1]
    Assert-Equal 'the last authentication reaches the endpoint' 'jdoe' $joined[0].auth_identity
    Assert-Equal 'and brings the session context with it' 'core-1' $joined[0].auth_nad
    Assert-Equal 'including what the screen calls authorization profiles' `
        'PermitAccess' $joined[1].auth_profiles
    # AABB.CC44.5566 against AA:BB:CC:44:55:66 -- same endpoint, two spellings.
    Assert-Equal 'a differently punctuated MAC still joins' 'asmith' $joined[1].auth_identity
    Assert-That 'an endpoint that did not authenticate gets an empty column, not a wrong one' (
        $null -eq $joined[2].auth_identity)
    Assert-That 'and the column exists so the blank is legible' (
        $joined[2].PSObject.Properties.Name -contains 'auth_identity')

    $before = $listenerState.Requests.Count
    $viaPxGrid = @(Get-IseContextVisibility -Mac 'AA:BB:CC:11:22:33' -ViaPxGrid)
    Assert-Equal '-ViaPxGrid costs one endpoint read plus one free session read' 2 (
        $listenerState.Requests.Count - $before)
    Assert-Equal 'and the second read is pxGrid, not a second Oracle statement' `
        '/api/v1/pxgrid/sessions?mac=AA%3ABB%3ACC%3A11%3A22%3A33' `
        $listenerState.Requests[-1]
    Assert-Equal 'the live session lands in its own namespace' `
        'jdoe' $viaPxGrid[0].session_user_name
    Assert-Equal 'and the network device' 'campus-corp-wired' $viaPxGrid[0].session_nad
    Assert-Equal 'and what the session was authorized as' `
        'PermitAccess' $viaPxGrid[0].session_authorization_profiles
    Assert-Equal 'the real authMethod is used, not the session state' `
        'dot1x' $viaPxGrid[0].session_auth_method
    Assert-Equal 'and the table still has an identity to name' 'jdoe' $viaPxGrid[0].identity
    Assert-Equal 'and a device' 'campus-corp-wired' $viaPxGrid[0].nad

    # -First bounds the endpoint rows. The session list is a different set,
    # sorted by MAC and cut at the route's ceiling, so forwarding it would
    # fetch the N lowest-MAC sessions on the appliance rather than the sessions
    # belonging to these N endpoints -- and every endpoint past the cut would
    # come back with empty session_* columns and no way to tell why.
    $null = Get-IseContextVisibility -Mac 'AA:BB:CC:11:22:33' -ViaPxGrid -First 2 `
        -WarningAction SilentlyContinue
    Assert-That 'the endpoint row limit does not become the session ceiling' (
        $listenerState.Requests[-1] -notmatch 'first=')

    # Measured on a live appliance: 50 of 53 probe attributes have no pxGrid
    # counterpart and 28 of 31 pxGrid fields have no probe counterpart, so
    # making these exclusive threw away most of what is knowable.
    $both = @(Get-IseContextVisibility -Mac 'AA:BB:CC:11:22:33' -WithLastAuth -ViaPxGrid `
        -WarningAction SilentlyContinue)
    Assert-Equal 'auth_* survives alongside session_*' 'jdoe' $both[0].auth_identity
    Assert-Equal 'and session_* alongside auth_*' 'jdoe' $both[0].session_user_name

    $full = @(Get-IseContextVisibility -Mac 'AA:BB:CC:11:22:33' -Full `
        -WarningAction SilentlyContinue)
    Assert-Equal '-Full brings the profiling attributes too' `
        'Cisco Systems, Inc' $full[0].probe_oui
    Assert-Equal 'with ISE spellings normalised into properties' `
        'campus-corp-wired' $full[0].probe_networkdevicename
    Assert-That '-Full reaches all three sources at once' (
        $full[0].auth_identity -and $full[0].session_user_name -and $full[0].probe_oui)
    # PROBE_DATA is already on the row this cmdlet fetches, so surfacing it
    # costs no extra statement.
    $before = $listenerState.Requests.Count
    $null = Get-IseContextVisibility -Mac 'AA:BB:CC:11:22:33' -WithProbe `
        -WarningAction SilentlyContinue
    Assert-Equal '-WithProbe costs nothing extra' 1 (
        $listenerState.Requests.Count - $before)
    Assert-That 'an empty profiling attribute is not made into a column' (
        $full[0].PSObject.Properties.Name -notcontains 'probe_assethwrevision')

    # An exporter without pxGrid has to say so and name the alternative, not
    # hand back an HTTP code and let the operator guess.
    $refusal = $null
    try { Get-IseContextVisibility -Mac 'refuse' -ViaPxGrid } catch { $refusal = $_ }
    Assert-Like 'an exporter without pxGrid says so and names the alternative' `
        '*no pxGrid target configured*-WithLastAuth*' "$($refusal.Exception.Message)"

    # MnT has no bulk form, so this is one request per endpoint. It carries the
    # accounting counters and correlation ids nothing else does.
    $before = $listenerState.Requests.Count
    $withSession = @(Get-IseContextVisibility -Mac 'AA:BB:CC:11:22:33' -WithSession `
        -WarningAction SilentlyContinue)
    # The stub answers every query with all three endpoints, so this is one
    # endpoint read plus one MnT read each -- which is the shape that matters:
    # per endpoint, not per result set.
    Assert-Equal '-WithSession costs one MnT read per endpoint' 4 (
        $listenerState.Requests.Count - $before)
    Assert-Like 'and the MnT read names the endpoint' '*mnt/session?mac=*' `
        $listenerState.Requests[-1]
    Assert-Equal 'accounting counters arrive, and nothing else has them' `
        '84213' $withSession[0].mnt_acct_input_octets
    Assert-Equal 'so does the correlation id pxGrid lacks' `
        '0B0B0B0B00000001' $withSession[0].mnt_audit_session_id
    Assert-That 'an empty MnT field is not made into a column' (
        $withSession[0].PSObject.Properties.Name -notcontains 'mnt_empty_on_purpose')

    # Three endpoints, a limit of one: the refusal has to happen before the
    # requests, not be discovered in the output afterwards.
    $before = $listenerState.Requests.Count
    $warnings = @()
    $capped = @(Get-IseContextVisibility -WithSession -SessionLimit 1 `
        -WarningVariable warnings)
    Assert-Equal 'the limit bounds the requests, not the rows' 2 (
        $listenerState.Requests.Count - $before)
    Assert-Equal 'every endpoint still comes back' 3 $capped.Count
    Assert-Like 'and the shortfall is said out loud' '*no bulk form*' (
        $warnings -join ' ')

    # -Full is the sources that cost nothing per endpoint: the endpoint read,
    # one authentication read and one free pxGrid read. Per-endpoint work has
    # to be asked for by name.
    $before = $listenerState.Requests.Count
    $null = Get-IseContextVisibility -Mac 'AA:BB:CC:11:22:33' -Full `
        -WarningAction SilentlyContinue
    $issued = @($listenerState.Requests | Select-Object -Last (
        $listenerState.Requests.Count - $before))
    Assert-Equal '-Full is three reads for the whole result, not per endpoint' 3 `
        $issued.Count
    Assert-That '-Full does not quietly enable the per-endpoint MnT fetch' (
        @($issued | Where-Object { $_ -like '*mnt/session*' }).Count -eq 0)

    $probe = @(Get-IseEndpointProbe -Mac 'AA:BB:CC:11:22:33' -WarningAction SilentlyContinue)
    Assert-Equal 'the probe read fetches only the two columns it needs' (
        '/api/v1/dataconnect/query?cols=MAC_ADDRESS%2CPROBE_DATA' +
        '&eq=MAC_ADDRESS%3AAA%3ABB%3ACC%3A11%3A22%3A33&view=endpoints_data'
    ) $listenerState.Requests[-1]
    Assert-Equal 'one row per attribute' 2 $probe.Count
    Assert-Equal 'attributes are typed for their own table' `
        'Ise.Dc.ProbeAttribute' $probe[0].PSObject.TypeNames[0]
    Assert-Equal 'and sorted by name' 'NetworkDeviceName' $probe[0].Name
    Assert-Equal 'carrying the value' 'Cisco Systems, Inc' $probe[1].Value
    Assert-That 'the endpoint stays on every row so a wildcard read is readable' (
        $probe[0].Mac -eq 'AA:BB:CC:11:22:33')

    # ISE names an attribute whether or not it has a value, so a real endpoint
    # carries dozens of blanks; showing them buries the ones that say something.
    Assert-That 'empty attributes are hidden' (
        @($probe | Where-Object { $_.Name -eq 'assetHwRevision' }).Count -eq 0)
    $withEmpty = @(Get-IseEndpointProbe -Mac 'AA:BB:CC:11:22:33' -IncludeEmpty `
        -WarningAction SilentlyContinue)
    Assert-Equal '-IncludeEmpty puts them back' 3 $withEmpty.Count

    $named = @(Get-IseEndpointProbe -Name '*Device*' -WarningAction SilentlyContinue)
    Assert-Equal '-Name filters attributes' 1 $named.Count
    Assert-Equal 'and keeps the one asked for' 'NetworkDeviceName' $named[0].Name

    $flat = Get-IseEndpointProbe -Mac 'AA:BB:CC:11:22:33' -AsObject `
        -WarningAction SilentlyContinue
    Assert-Equal '-AsObject makes attributes properties' 'Cisco Systems, Inc' $flat.OUI

    # The truncation is the appliance's, and an operator reading three
    # attributes must not believe that is all ISE knows.
    $warnings = @()
    $null = Get-IseEndpointProbe -Mac 'AA:BB:CC:11:22:33' -WarningVariable warnings
    Assert-Like 'a truncated probe field warns rather than passing off a prefix' `
        '*not reachable through Data Connect*' ($warnings -join ' ')

    Write-Host ''
    Write-Host 'refusals' -ForegroundColor Cyan

    $message = try { Invoke-IseDcQuery -View cooldown_view; '' } catch { $_.Exception.Message }
    Assert-Like 'a cooldown says how long to wait' '*cooling down; retry in 38s*' $message

    $record = try { Invoke-IseDcQuery -View cooldown_view; $null } catch { $_ }
    Assert-Equal 'the refusal rides on the error record' 38 $record.TargetObject.retry_after_seconds

    $message = try { Invoke-IseDcQuery -View busy_view; '' } catch { $_.Exception.Message }
    Assert-Like 'busy explains single flight' '*one at a time*' $message

    $message = try { Invoke-IseDcQuery -View pending_view; '' } catch { $_.Exception.Message }
    Assert-Like 'schema_pending names the catalog' '*catalog*' $message
    Assert-Like 'schema_pending keeps the server detail' '*discovery has not completed*' $message

    $message = try { Invoke-IseDcQuery -View nosuch_view; '' } catch { $_.Exception.Message }
    Assert-Like 'unknown_view points at Get-IseDcView' '*Get-IseDcView*' $message

    $unreachable = try {
        Invoke-IseApi -Path '/api/v1/health' -TimeoutSec 2 -ErrorAction Stop
        ''
    } catch { $_.Exception.Message }
    Assert-Like 'a 404 is not mistaken for an unreachable exporter' '*HTTP 404*' $unreachable

    $before = $listenerState.Requests.Count
    $waited = @(Invoke-IseDcQuery -View flaky_view -Wait)
    Assert-Equal '-Wait retries after a cooldown' 2 ($listenerState.Requests.Count - $before)
    Assert-Equal '-Wait returns the rows it waited for' 1 $waited.Count

    Write-Host ''
    Write-Host 'formatting' -ForegroundColor Cyan

    $table = @(Get-IseDcView | Format-Table | Out-String -Width 200) -join ''
    Assert-Like 'the view table renders its own columns' '*Avail*' $table
    $table = @(Invoke-IseDcQuery -View radius_authentications | Format-Table | Out-String -Width 200) -join ''
    Assert-Like 'the auth table leads with time and user' '*Time*User*Mac*Nad*' $table
    Assert-That 'the auth table does not wrap' (
        (($table -split "`n" | Measure-Object -Property Length -Maximum).Maximum) -le 120)

    # The replicas take their column order from the UI, so they are the tables
    # most likely to grow past a terminal; the width is asserted, not assumed.
    $table = @(Get-IseRadiusLiveLog -Last 1h | Format-Table | Out-String -Width 200) -join ''
    Assert-Like 'the live-log table reads like the screen' '*Time*Status*Identity*Endpoint*' $table
    Assert-That 'the live-log table does not wrap' (
        (($table -split "`n" | Measure-Object -Property Length -Maximum).Maximum) -le 120)

    $table = @(Get-IseContextVisibility | Format-Table | Out-String -Width 200) -join ''
    Assert-Like 'the context table leads with the endpoint' '*Mac*Ip*Hostname*Profile*' $table
    Assert-That 'the context table does not wrap' (
        (($table -split "`n" | Measure-Object -Property Length -Maximum).Maximum) -le 120)
}
finally {
    try { Invoke-RestMethod -Uri "$root/shutdown" -TimeoutSec 5 | Out-Null } catch { }
    try { $server.EndInvoke($serverHandle) } catch { }
    $server.Dispose()
    $runspace.Dispose()
    Remove-Item Env:\ISE_EXPORTER_API -ErrorAction SilentlyContinue
}

Write-Host ''
if ($listenerState['Error']) {
    Write-Host "stub listener error: $($listenerState['Error'])" -ForegroundColor Red
    $script:Failed++
}
Write-Host "$script:Passed passed, $script:Failed failed" -ForegroundColor $(
    if ($script:Failed) { 'Red' } else { 'Green' })
Write-Host ''
exit ([int]($script:Failed -gt 0))
