@{
    RootModule        = 'Ise.Cli3.psm1'
    ModuleVersion     = '3.1.0'
    GUID              = 'a3f1c6d2-5b7e-4c19-9f82-6d4e1b0a7c53'
    Author            = 'Joshua Johnson'
    Description       = 'Read-only operator cmdlets for ise-exporter3, over its local API.'
    PowerShellVersion = '7.0'
    # Loaded with the module rather than by the profile: a script that imports
    # the manifest directly should get the same readable tables the REPL does.
    FormatsToProcess  = 'Ise.Cli3.format.ps1xml'
    FunctionsToExport = @(
        'Get-IseApiRoot', 'Set-IseApiRoot', 'Invoke-IseApi',
        'Get-IseHealth', 'Get-IseDataset', 'Get-IseProvider',
        'Get-IseTarget', 'Get-IsePlan', 'Get-IseDegraded',
        # Everything below reaches Oracle through the exporter's paced transport.
        'Get-IseDcView', 'Get-IseDcColumn', 'Get-IseDcStatus', 'Invoke-IseDcQuery',
        'Get-IseDcRadiusAuth', 'Get-IseDcRadiusAccounting', 'Get-IseDcRadiusError',
        'Get-IseDcEndpoint', 'Get-IseDcTacacsAuth', 'Get-IseDcTacacsCommand',
        'Get-IseDcTacacsAuthorization', 'Get-IseDcPosture', 'Get-IseDcNodeHealth',
        'Get-IseDcNodePerformance'
    )
    CmdletsToExport   = @()
    VariablesToExport = @()
    AliasesToExport   = @()
    PrivateData       = @{
        PSData = @{
            Tags = @('Cisco', 'ISE', 'Prometheus', 'Monitoring')
        }
    }
}
