[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$Python = 'python',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$BootstrapArguments
)

$ErrorActionPreference = 'Stop'
& $Python (Join-Path $PSScriptRoot 'bootstrap.py') @BootstrapArguments
exit $LASTEXITCODE
