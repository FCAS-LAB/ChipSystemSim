<#
.SYNOPSIS
Creates a verified Ubuntu 22.04 VMware base VM for LEGOSim Swarm workers.

.DESCRIPTION
Imports Ubuntu's official cloud OVA, validates its SHA-256 manifest, then
attaches a NoCloud seed ISO. Cloud-init installs Docker, SSH and open-vm-tools
and creates a private legosim administrator for vmrun automation.
#>
[CmdletBinding()]
param(
    [string]$VmRoot = 'E:\ChipSystemSimVMs',
    [string]$VmwareRoot = 'E:\VMware',
    [int]$MemoryMiB = 1536,
    [int]$Processors = 2
)

$ErrorActionPreference = 'Stop'

function Require-File([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file is missing: $Path"
    }
}

function New-PrivatePassword {
    # Alphanumeric output avoids cloud-init and vmrun quoting ambiguity.
    $alphabet = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $bytes = New-Object byte[] 28
        $rng.GetBytes($bytes)
        return -join ($bytes | ForEach-Object { $alphabet[$_ % $alphabet.Length] })
    }
    finally {
        $rng.Dispose()
    }
}

$imagesDirectory = Join-Path $VmRoot 'images'
$baseDirectory = Join-Path $VmRoot 'base'
$seedDirectory = Join-Path $VmRoot 'seed'
$secretsDirectory = Join-Path $VmRoot 'secrets'
$ovaPath = Join-Path $imagesDirectory 'jammy-server-cloudimg-amd64.ova'
$sumsPath = Join-Path $imagesDirectory 'SHA256SUMS'
$seedIso = Join-Path $seedDirectory 'legosim-base-seed.iso'
$vmxPath = Join-Path $baseDirectory 'legosim-base.vmx'
$passwordPath = Join-Path $secretsDirectory 'legosim-guest-password.txt'
$ovfTool = Join-Path $VmwareRoot 'OVFTool\ovftool.exe'
$vmrun = Join-Path $VmwareRoot 'vmrun.exe'
$mkisofs = Join-Path $VmwareRoot 'mkisofs.exe'

Require-File $ovaPath
Require-File $sumsPath
Require-File $ovfTool
Require-File $vmrun
Require-File $mkisofs

$expectedLine = Get-Content -LiteralPath $sumsPath |
    # GNU checksum manifests prefix binary file names with an optional '*'.
    Where-Object { $_ -match '\s\*?jammy-server-cloudimg-amd64\.ova$' } |
    Select-Object -First 1
if (-not $expectedLine) { throw 'SHA256SUMS does not contain the Ubuntu OVA' }
$expectedHash = ($expectedLine -split '\s+')[0].ToLowerInvariant()
$actualHash = (Get-FileHash -LiteralPath $ovaPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($expectedHash -ne $actualHash) {
    throw "OVA SHA-256 mismatch: expected $expectedHash, got $actualHash"
}

New-Item -ItemType Directory -Force -Path $baseDirectory, $seedDirectory, $secretsDirectory | Out-Null
if (-not (Test-Path -LiteralPath $passwordPath)) {
    $password = New-PrivatePassword
    Set-Content -LiteralPath $passwordPath -Value $password -NoNewline -Encoding ascii
}
else {
    $password = Get-Content -LiteralPath $passwordPath -Raw
}

$userData = @"
#cloud-config
hostname: legosim-base
manage_etc_hosts: true
ssh_pwauth: true
write_files:
  - path: /etc/ssh/sshd_config.d/99-legosim-password.conf
    owner: root:root
    permissions: '0644'
    content: |
      PasswordAuthentication yes
      KbdInteractiveAuthentication no
      UsePAM yes
users:
  - default
  - name: legosim
    groups: [sudo, docker]
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: false
chpasswd:
  expire: false
  list: |
    legosim:$password
package_update: true
packages:
  - docker.io
  - openssh-server
  - open-vm-tools
runcmd:
  - systemctl enable --now docker
  - systemctl restart ssh
  - usermod -aG docker legosim
  - mkdir -p /opt/chipsystemsim-swarm
  - touch /var/lib/legosim-bootstrap-ready
"@
# Bump this whenever the seed schema changes.  NoCloud then applies the
# corrected configuration to an already-created base disk on its next boot.
$metaData = "instance-id: legosim-base-ssh-v4`nlocal-hostname: legosim-base`n"
# NoCloud requires the first bytes of user-data to be "#cloud-config".  Windows
# PowerShell's UTF-8 encoding writes a BOM, so use an explicitly BOM-free writer.
$utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText((Join-Path $seedDirectory 'user-data'), $userData, $utf8WithoutBom)
[System.IO.File]::WriteAllText((Join-Path $seedDirectory 'meta-data'), $metaData, $utf8WithoutBom)
# The Windows mkisofs build turns absolute source paths into mangled ISO names.
# Build from the seed directory so NoCloud sees exactly user-data and meta-data.
Push-Location $seedDirectory
try {
    & $mkisofs -o $seedIso -V cidata -r 'user-data' 'meta-data'
}
finally {
    Pop-Location
}
if ($LASTEXITCODE -ne 0) { throw 'mkisofs failed while creating the seed ISO' }

if (-not (Test-Path -LiteralPath $vmxPath)) {
    & $ovfTool --acceptAllEulas --overwrite --name='legosim-base' $ovaPath $vmxPath
    if ($LASTEXITCODE -ne 0) { throw 'OVF Tool failed to import the Ubuntu OVA' }
}

# The OVA's generated VMX contains legacy physical serial/floppy device
# descriptors that this Workstation installation rejects.  Keep the imported
# VMDK but replace that descriptor with the minimal, host-independent VMX.
$seedIsoVmx = $seedIso.Replace('\', '/')
$diskFile = [System.IO.Path]::GetFileName((Get-ChildItem -LiteralPath $baseDirectory -Filter '*-disk1.vmdk' -File | Select-Object -First 1).FullName)
if (-not $diskFile) { throw 'Imported OVA did not create a boot VMDK' }
$cleanVmx = @"
.encoding = "UTF-8"
config.version = "8"
virtualHW.version = "10"
displayName = "legosim-base"
guestOS = "ubuntu-64"
firmware = "bios"
memsize = "$MemoryMiB"
numvcpus = "$Processors"
scsi0.present = "TRUE"
scsi0.virtualDev = "lsilogic"
scsi0:0.present = "TRUE"
scsi0:0.deviceType = "disk"
scsi0:0.fileName = "$diskFile"
ethernet0.present = "TRUE"
ethernet0.connectionType = "nat"
ethernet0.vnet = "VMnet8"
# e1000 works with the imported hardware-version-10 PCI topology.  e1000e
# requires an explicit PCIe root port there and causes Workstation to abort.
ethernet0.virtualDev = "e1000"
ethernet0.addressType = "generated"
ethernet0.startConnected = "TRUE"
ide1:0.present = "TRUE"
ide1:0.deviceType = "cdrom-image"
ide1:0.fileName = "$seedIsoVmx"
ide1:0.startConnected = "TRUE"
tools.syncTime = "FALSE"
guestinfo.legosim.cloudinit.seed = "$seedIsoVmx"
"@
Set-Content -LiteralPath $vmxPath -Value $cleanVmx -NoNewline -Encoding ascii

& $vmrun -T ws start $vmxPath nogui
if ($LASTEXITCODE -ne 0) { throw 'vmrun could not start the base VM' }

$deadline = (Get-Date).AddMinutes(15)
do {
    Start-Sleep -Seconds 5
    $guestIp = (& $vmrun -T ws getGuestIPAddress $vmxPath -wait 2>$null).Trim()
    if ($guestIp -and $guestIp -notmatch '^169\.254\.') { break }
} while ((Get-Date) -lt $deadline)
if (-not $guestIp -or $guestIp -match '^169\.254\.') {
    throw 'Base VM did not receive a usable VMware NAT address within 15 minutes'
}

$guestDeadline = (Get-Date).AddMinutes(15)
do {
    & $vmrun -T ws -gu legosim -gp $password runProgramInGuest $vmxPath /usr/bin/test -f /var/lib/legosim-bootstrap-ready 2>$null
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 10
} while ((Get-Date) -lt $guestDeadline)
if ($LASTEXITCODE -ne 0) { throw 'cloud-init did not finish within 15 minutes' }

[pscustomobject]@{
    VmName = 'legosim-base'
    VmxPath = $vmxPath
    GuestIp = $guestIp
    DockerReady = $true
    CredentialFile = $passwordPath
} | Format-List
