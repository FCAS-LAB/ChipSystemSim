[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$VmxPath,
    [Parameter(Mandatory)]
    [ValidatePattern('^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$')]
    [string]$MacAddress,
    [Parameter(Mandatory)]
    [ValidatePattern('^192\.168\.244\.(?:[3-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-4])$')]
    [string]$Address,
    [string]$VmwareRoot = 'E:\VMware'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$vmrun = Join-Path $VmwareRoot 'vmrun.exe'
$mkisofs = Join-Path $VmwareRoot 'mkisofs.exe'
foreach ($path in @($VmxPath, $vmrun, $mkisofs)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required input is missing: $path" }
}

$nodeDirectory = Split-Path -Parent $VmxPath
$nodeName = [System.IO.Path]::GetFileNameWithoutExtension($VmxPath)
$seedDirectory = Join-Path $nodeDirectory 'network-rescue-seed'
$seedIso = Join-Path $seedDirectory 'seed.iso'
$instanceId = "$nodeName-network-rescue-$(Get-Date -Format 'yyyyMMddHHmmss')"
$utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)

New-Item -ItemType Directory -Force -Path $seedDirectory | Out-Null
$metaData = "instance-id: $instanceId`nlocal-hostname: $nodeName`n"
$userData = @"
#cloud-config
hostname: $nodeName
manage_etc_hosts: true
"@
$networkConfig = @"
version: 2
ethernets:
  enp0s17:
    match:
      macaddress: "$MacAddress"
    set-name: enp0s17
    addresses: [$Address/24]
    routes:
      - to: default
        via: 192.168.244.2
    nameservers:
      # VMnet8's NAT gateway routes traffic but does not provide DNS service.
      addresses: [8.8.8.8]
"@
[System.IO.File]::WriteAllText((Join-Path $seedDirectory 'user-data'), $userData, $utf8WithoutBom)
[System.IO.File]::WriteAllText((Join-Path $seedDirectory 'meta-data'), $metaData, $utf8WithoutBom)
[System.IO.File]::WriteAllText((Join-Path $seedDirectory 'network-config'), $networkConfig, $utf8WithoutBom)

Push-Location $seedDirectory
try {
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $mkisofs -o $seedIso -V cidata -r 'user-data' 'meta-data' 'network-config' *> $null
    $mkisofsExit = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorAction
}
finally { Pop-Location }
if ($mkisofsExit -ne 0) { throw 'Could not create network-rescue ISO' }

# VMware cannot change the attached CD while the VM is running.  Stop only this
# explicitly named node, replace its NoCloud seed, then boot it again.
& $vmrun -T ws stop $VmxPath soft 2>$null
Start-Sleep -Seconds 3
$vmxText = Get-Content -LiteralPath $VmxPath -Raw
$seedIsoVmx = $seedIso.Replace('\', '/')
# Remove stale VMware cloud-init data. The NoCloud CD below is the single
# authoritative source for this recovery boot.
$vmxText = [regex]::Replace($vmxText, '(?m)^guestinfo\.(metadata|userdata)(\.encoding)?\s*=.*\r?\n?', '')
if ($vmxText -match '(?m)^ide1:0\.fileName\s*=') {
    $vmxText = [regex]::Replace($vmxText, '(?m)^ide1:0\.fileName\s*=\s*".*"$', "ide1:0.fileName = `"$seedIsoVmx`"")
}
else {
    $vmxText += "`nide1:0.fileName = `"$seedIsoVmx`"`n"
}
if ($vmxText -notmatch '(?m)^ide1:0\.present\s*=') {
    $vmxText += "ide1:0.present = `"TRUE`"`n"
}
if ($vmxText -notmatch '(?m)^ide1:0\.deviceType\s*=') {
    $vmxText += "ide1:0.deviceType = `"cdrom-image`"`n"
}
if ($vmxText -notmatch '(?m)^ide1:0\.startConnected\s*=') {
    $vmxText += "ide1:0.startConnected = `"TRUE`"`n"
}
[System.IO.File]::WriteAllText($VmxPath, $vmxText, $utf8WithoutBom)

& $vmrun -T ws start $VmxPath nogui
if ($LASTEXITCODE -ne 0) { throw 'Could not start node with static-network rescue seed' }
Write-Output "Static rescue seed attached for $nodeName at $Address"
