<#
.SYNOPSIS
Moves the host-local VMware asset root from LegoSimbricksVMs to ChipSystemSimVMs.

.DESCRIPTION
VMX files use relative disk and seed paths, so moving the single top-level root
preserves the VM contents. VMware Workstation must not have a VM from that root
running. This script does not rename guest hostnames, MAC addresses, UUIDs, or
the legacy `legosim` guest account because they are compatibility identities,
not project branding.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$SourceRoot = 'E:\LegoSimbricksVMs',
    [string]$DestinationRoot = 'E:\ChipSystemSimVMs',
    [string]$VmwareRoot = 'E:\VMware'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
    throw "Source VMware root does not exist: $SourceRoot"
}
if (Test-Path -LiteralPath $DestinationRoot) {
    throw "Destination VMware root already exists: $DestinationRoot"
}

$vmrun = Join-Path $VmwareRoot 'vmrun.exe'
if (-not (Test-Path -LiteralPath $vmrun -PathType Leaf)) {
    throw "vmrun.exe is required to verify powered-off VMs: $vmrun"
}

$sourcePrefix = $SourceRoot.TrimEnd('\') + '\'
$running = @(& $vmrun -T ws list 2>$null | Select-Object -Skip 1 |
    Where-Object { $_.StartsWith($sourcePrefix, [System.StringComparison]::OrdinalIgnoreCase) })
if ($running.Count -gt 0) {
    $runningList = $running -join "`n"
    throw "Refusing to move a VMware root containing running VMs:`n$runningList"
}

if ($PSCmdlet.ShouldProcess($SourceRoot, "Move VMware asset root to $DestinationRoot")) {
    Move-Item -LiteralPath $SourceRoot -Destination $DestinationRoot
}

Write-Output "VMware asset root migrated to: $DestinationRoot"
Write-Output 'Re-open the VMX files from their new paths in VMware Workstation if they are not already listed.'
