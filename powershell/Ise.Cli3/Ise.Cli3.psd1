@{
    RootModule        = 'Ise.Cli3.psm1'
    ModuleVersion     = '3.0.0'
    GUID              = 'a3f1c6d2-5b7e-4c19-9f82-6d4e1b0a7c53'
    Author            = 'Joshua Johnson'
    Description       = 'Read-only operator cmdlets for ise-exporter3, over its local API.'
    PowerShellVersion = '7.0'
    FunctionsToExport = @(
        'Get-IseApiRoot', 'Set-IseApiRoot', 'Invoke-IseApi',
        'Get-IseHealth', 'Get-IseDataset', 'Get-IseProvider',
        'Get-IseTarget', 'Get-IsePlan', 'Get-IseDegraded'
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
