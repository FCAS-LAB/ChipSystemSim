<#
.SYNOPSIS
Verifies or applies a versioned VMware resource profile to LEGOSim fresh nodes.

.DESCRIPTION
The profile records only portable VM settings. It deliberately preserves each
VM's MAC address, UUID, disks, cloud-init seed and credentials. By default the
script is read-only and reports mismatches. Use -Apply only after the selected
VMs have been shut down.
#>
[CmdletBinding()]
param(
    [string]$ProfilePath,
    [string]$VmRoot,
    [string]$VmwareRoot = 'E:\VMware',
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $ProfilePath) {
    $ProfilePath = Join-Path $PSScriptRoot '..\vm\profiles\vmware-fresh-8x-1vcpu-1536mib.json'
}

function Get-VmxSetting {
    param(
        [Parameter(Mandatory)][string]$VmxText,
        [Parameter(Mandatory)][string]$Key
    )

    $pattern = '(?m)^' + [regex]::Escape($Key) + '\s*=\s*"([^"]*)"\s*$'
    $match = [regex]::Match($VmxText, $pattern)
    if ($match.Success) { return $match.Groups[1].Value }
    return $null
}

function Set-VmxSetting {
    param(
        [Parameter(Mandatory)][string]$VmxText,
        [Parameter(Mandatory)][string]$Key,
        [Parameter(Mandatory)][string]$Value
    )

    $line = "$Key = `"$Value`""
    $pattern = '(?m)^' + [regex]::Escape($Key) + '\s*=\s*"[^"]*"\s*$'
    if ([regex]::IsMatch($VmxText, $pattern)) {
        return [regex]::Replace($VmxText, $pattern, $line)
    }
    return $VmxText.TrimEnd("`r", "`n") + "`r`n$line`r`n"
}

function Test-VmRunning {
    param(
        [Parameter(Mandatory)][string]$VmxPath,
        [Parameter(Mandatory)][string]$VmrunPath
    )

    $runningVms = @(& $VmrunPath -T ws list 2>$null | Select-Object -Skip 1)
    return $runningVms -contains $VmxPath
}

$resolvedProfilePath = (Resolve-Path -LiteralPath $ProfilePath).Path
$profile = Get-Content -LiteralPath $resolvedProfilePath -Raw | ConvertFrom-Json
if ($profile.schema_version -ne 1) { throw "Unsupported profile schema: $($profile.schema_version)" }
if (-not $VmRoot) { $VmRoot = $profile.vm_root_default }
if (-not (Test-Path -LiteralPath $VmRoot -PathType Container)) { throw "VM root does not exist: $VmRoot" }

$vmrun = Join-Path $VmwareRoot 'vmrun.exe'
if ($Apply -and -not (Test-Path -LiteralPath $vmrun -PathType Leaf)) {
    throw "vmrun.exe is required for -Apply: $vmrun"
}

$expected = [ordered]@{
    'displayName' = $null
    'guestOS' = [string]$profile.hardware.guest_os
    'memsize' = [string]$profile.resources.memory_mib
    'numvcpus' = [string]$profile.resources.vcpus
    'ethernet0.connectionType' = [string]$profile.hardware.ethernet0_connection_type
    'ethernet0.vnet' = [string]$profile.hardware.ethernet0_vnet
    'ethernet0.virtualDev' = [string]$profile.hardware.ethernet0_virtual_device
}

$results = for ($slot = 0; $slot -lt [int]$profile.node_count; ++$slot) {
    $name = $profile.node_name_pattern.Replace('{slot}', [string]$slot)
    $vmxPath = Join-Path $VmRoot (Join-Path $profile.node_directory "$name\$name.vmx")
    if (-not (Test-Path -LiteralPath $vmxPath -PathType Leaf)) {
        [pscustomobject]@{ Slot = $slot; Name = $name; State = 'missing'; Changed = $false; Mismatches = 'VMX file not found' }
        continue
    }

    $vmxText = Get-Content -LiteralPath $vmxPath -Raw
    $expected['displayName'] = $name
    $mismatches = @()
    foreach ($entry in $expected.GetEnumerator()) {
        $actual = Get-VmxSetting -VmxText $vmxText -Key $entry.Key
        if ($actual -ne $entry.Value) { $mismatches += "$($entry.Key): expected '$($entry.Value)', got '$actual'" }
    }

    $changed = $false
    if ($Apply -and $mismatches.Count -gt 0) {
        if (Test-VmRunning -VmxPath $vmxPath -VmrunPath $vmrun) {
            throw "Refusing to modify running VM: $vmxPath. Shut it down, then rerun -Apply."
        }
        foreach ($entry in $expected.GetEnumerator()) {
            $vmxText = Set-VmxSetting -VmxText $vmxText -Key $entry.Key -Value $entry.Value
        }
        [System.IO.File]::WriteAllText($vmxPath, $vmxText, [System.Text.UTF8Encoding]::new($false))
        $changed = $true
    }

    [pscustomobject]@{
        Slot = $slot
        Name = $name
        State = if ($mismatches.Count -eq 0) { 'matches' } elseif ($Apply) { 'updated' } else { 'mismatch' }
        Changed = $changed
        Mismatches = if ($mismatches) { $mismatches -join '; ' } else { '' }
    }
}

$results | Format-Table -AutoSize
if (-not $Apply) {
    Write-Output 'Read-only verification completed. Use -Apply only while every target VM is shut down.'
}
$unresolved = @($results | Where-Object State -in @('missing', 'mismatch'))
if ($unresolved.Count -gt 0 -and -not $Apply) { exit 1 }
