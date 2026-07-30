[CmdletBinding()]
param(
    [ValidateRange(1, 8)]
    [int]$NodeCount = 1,
    [string]$VmRoot = 'E:\ChipSystemSimVMs',
    [string]$VmwareRoot = 'E:\VMware',
    [string]$SnapshotName = 'clean-docker-ready',
    [string]$RegistryAddress = '192.168.244.1:5000',
    [ValidateRange(768, 8192)] [int]$ManagerMemoryMiB = 1536,
    [ValidateRange(512, 8192)] [int]$WorkerMemoryMiB = 768
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$vmrun = Join-Path $VmwareRoot 'vmrun.exe'
$mkisofs = Join-Path $VmwareRoot 'mkisofs.exe'
$baseVmx = Join-Path $VmRoot 'base\legosim-base.vmx'
$nodesRoot = Join-Path $VmRoot 'nodes'
$passwordPath = Join-Path $VmRoot 'secrets\legosim-guest-password.txt'

foreach ($path in @($vmrun, $mkisofs, $baseVmx, $passwordPath)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required input is missing: $path" }
}
& python -c 'import paramiko' *> $null
if ($LASTEXITCODE -ne 0) { throw 'Install paramiko first: python -m pip install --user paramiko' }

function Invoke-Vmrun {
    param([Parameter(Mandatory)][string[]]$Arguments)
    & $vmrun @Arguments
    if ($LASTEXITCODE -ne 0) { throw "vmrun failed: $($Arguments -join ' ')" }
}

function Invoke-GuestSsh {
    param([Parameter(Mandatory)][string]$Address, [Parameter(Mandatory)][string]$Command)
    # The password stays in a local credential file rather than process arguments.
    $encoded = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($Command))
    $python = @'
from pathlib import Path
import base64, sys, paramiko
address, password_path, encoded = sys.argv[1:4]
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(address, username='legosim', password=Path(password_path).read_text(encoding='ascii').strip(), timeout=30, banner_timeout=30, auth_timeout=30)
stdin, stdout, stderr = client.exec_command(base64.b64decode(encoded).decode('utf-8'), timeout=180)
sys.stdout.write(stdout.read().decode('utf-8', errors='replace'))
sys.stderr.write(stderr.read().decode('utf-8', errors='replace'))
status = stdout.channel.recv_exit_status()
client.close()
sys.exit(status)
'@
    $python | python - $Address $passwordPath $encoded
    if ($LASTEXITCODE -ne 0) { throw "Guest command failed on $Address" }
}

function Wait-GuestAddress {
    param([Parameter(Mandatory)][string]$VmxPath)
    $deadline = (Get-Date).AddMinutes(5)
    do {
        $address = (& $vmrun -T ws getGuestIPAddress $VmxPath 2>$null).Trim()
        if ($address -and $address -notmatch '^169\.254\.') {
            try { Invoke-GuestSsh -Address $address -Command 'true'; return $address } catch { }
        }
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)
    throw "Guest did not become SSH-ready: $VmxPath"
}

function New-NodeSeed {
    param(
        [Parameter(Mandatory)][string]$NodeDirectory,
        [Parameter(Mandatory)][string]$NodeName,
        [Parameter(Mandatory)][string]$MacAddress
    )

    $seedDirectory = Join-Path $NodeDirectory 'seed'
    $seedIso = Join-Path $seedDirectory 'node-seed.iso'
    New-Item -ItemType Directory -Force -Path $seedDirectory | Out-Null
    $utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
    $userData = "#cloud-config`nhostname: $NodeName`nmanage_etc_hosts: true`n"
    $metaData = "instance-id: $NodeName`nlocal-hostname: $NodeName`n"
    $networkConfig = @"
version: 2
ethernets:
  enp0s17:
    match:
      macaddress: "$MacAddress"
    set-name: enp0s17
    dhcp4: true
    dhcp6: true
"@
    [System.IO.File]::WriteAllText((Join-Path $seedDirectory 'user-data'), $userData, $utf8WithoutBom)
    [System.IO.File]::WriteAllText((Join-Path $seedDirectory 'meta-data'), $metaData, $utf8WithoutBom)
    [System.IO.File]::WriteAllText((Join-Path $seedDirectory 'network-config'), $networkConfig, $utf8WithoutBom)
    Push-Location $seedDirectory
    try {
        # This Windows mkisofs build prints normal progress to stderr.  Preserve
        # strict error handling elsewhere and use its exit code here instead.
        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & $mkisofs -o $seedIso -V cidata -r 'user-data' 'meta-data' 'network-config' *> $null
        $mkisofsExit = $LASTEXITCODE
        $ErrorActionPreference = $previousErrorAction
    }
    finally {
        Pop-Location
    }
    if ($mkisofsExit -ne 0) { throw "Could not create NoCloud seed for $NodeName" }
    return $seedIso.Replace('\', '/')
}

function Set-CloneResources {
    param(
        [Parameter(Mandatory)][string]$VmxPath,
        [Parameter(Mandatory)][string]$DisplayName,
        [Parameter(Mandatory)][int]$MemoryMiB,
        [Parameter(Mandatory)][string]$MacAddress,
        [Parameter(Mandatory)][string]$SeedIso
    )
    $vmx = Get-Content -LiteralPath $VmxPath -Raw
    $vmx = $vmx -replace '(?m)^displayName\s*=\s*".*"\r?$', "displayName = `"$DisplayName`""
    $vmx = $vmx -replace '(?m)^memsize\s*=\s*".*"\r?$', "memsize = `"$MemoryMiB`""
    $vmx = $vmx -replace '(?m)^numvcpus\s*=\s*".*"\r?$', 'numvcpus = "1"'
    $vmx = $vmx -replace '(?m)^ethernet0\.addressType\s*=\s*".*"\r?$', 'ethernet0.addressType = "static"'
    $vmx = $vmx -replace '(?m)^ethernet0\.generatedAddress.*\r?\n?', ''
    $vmx = $vmx -replace '(?m)^ethernet0\.generatedAddressOffset.*\r?\n?', ''
    $vmx = $vmx -replace '(?m)^ethernet0\.address\s*=\s*".*"\r?$', "ethernet0.address = `"$MacAddress`""
    if ($vmx -notmatch '(?m)^ethernet0\.address\s*=') { $vmx += "`nethernet0.address = `"$MacAddress`"`n" }
    $vmx = $vmx -replace '(?m)^ide1:0\.fileName\s*=\s*".*"\r?$', "ide1:0.fileName = `"$SeedIso`""
    [System.IO.File]::WriteAllText($VmxPath, $vmx, [System.Text.UTF8Encoding]::new($false))
}

New-Item -ItemType Directory -Force -Path $nodesRoot | Out-Null
$nodes = @()
for ($slot = 0; $slot -lt $NodeCount; ++$slot) {
    $name = "legosim-node-$slot"
    $directory = Join-Path $nodesRoot $name
    $vmx = Join-Path $directory "$name.vmx"
    $mac = '00:0C:29:FA:00:{0:X2}' -f ($slot + 1)
    if (-not (Test-Path -LiteralPath $vmx)) {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
        Invoke-Vmrun @('-T', 'ws', 'clone', $baseVmx, $vmx, 'linked', "-snapshot=$SnapshotName")
    }
    $running = (& $vmrun -T ws list) -contains $vmx
    if ($running) { throw "$name is already running; stop it before reconfiguring its NoCloud network seed" }
    $memory = if ($slot -eq 0) { $ManagerMemoryMiB } else { $WorkerMemoryMiB }
    $seedIso = New-NodeSeed -NodeDirectory $directory -NodeName $name -MacAddress $mac
    Set-CloneResources -VmxPath $vmx -DisplayName $name -MemoryMiB $memory -MacAddress $mac -SeedIso $seedIso
    & $vmrun -T ws start $vmx nogui *> $null
    if ($LASTEXITCODE -ne 0 -and -not ((& $vmrun -T ws list) -contains $vmx)) { throw "Could not start $name" }
    $nodes += [pscustomobject]@{ Slot = $slot; Name = $name; Address = (Wait-GuestAddress -VmxPath $vmx); VmxPath = $vmx }
}

foreach ($node in $nodes) {
    $json = '{"insecure-registries":["' + $RegistryAddress + '"]}'
    $command = "sudo hostnamectl set-hostname $($node.Name); echo '$json' | sudo tee /etc/docker/daemon.json >/dev/null; sudo systemctl restart docker"
    Invoke-GuestSsh -Address $node.Address -Command $command
}

$manager = $nodes | Where-Object Slot -eq 0
Invoke-GuestSsh -Address $manager.Address -Command "sudo docker swarm init --advertise-addr $($manager.Address)"
$joinToken = (Invoke-GuestSsh -Address $manager.Address -Command 'sudo docker swarm join-token -q worker').Trim()
if (-not $joinToken) { throw 'Swarm manager did not return a worker join token' }
foreach ($worker in ($nodes | Where-Object Slot -gt 0)) {
    Invoke-GuestSsh -Address $worker.Address -Command "sudo docker swarm join --token $joinToken $($manager.Address):2377"
}

$deadline = (Get-Date).AddMinutes(3)
do {
    $lines = @(Invoke-GuestSsh -Address $manager.Address -Command "sudo docker node ls --format '{{.ID}} {{.Hostname}} {{.Status}}'" -split "`r?`n" | Where-Object { $_ -match ' Ready$' })
    if ($lines.Count -eq $nodes.Count) { break }
    Start-Sleep -Seconds 5
} while ((Get-Date) -lt $deadline)
if ($lines.Count -ne $nodes.Count) { throw "Expected $($nodes.Count) Ready nodes, got $($lines.Count)" }
foreach ($node in $nodes) {
    $line = $lines | Where-Object { $_ -match " $([regex]::Escape($node.Name)) Ready$" } | Select-Object -First 1
    if (-not $line) { throw "Could not find node ID for $($node.Name)" }
    $id = ($line -split '\s+')[0]
    Invoke-GuestSsh -Address $manager.Address -Command "sudo docker node update --label-add chipsystemsim.node.$($node.Slot)=true $id >/dev/null"
}

Write-Output 'VMware Docker Swarm is ready:'
$nodes | Select-Object Slot, Name, Address, VmxPath | Format-Table -AutoSize
Invoke-GuestSsh -Address $manager.Address -Command 'sudo docker node ls'
