[CmdletBinding()]
param(
    [ValidateRange(0, 15)]
    [int]$Slot = 0,
    [string]$VmRoot = 'E:\ChipSystemSimVMs',
    [string]$VmwareRoot = 'E:\VMware',
    [ValidateRange(512, 8192)]
    [int]$MemoryMiB = 1536,
    [ValidateRange(1, 8)]
    [int]$Processors = 2
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ovfTool = Join-Path $VmwareRoot 'OVFTool\ovftool.exe'
$vmrun = Join-Path $VmwareRoot 'vmrun.exe'
$mkisofs = Join-Path $VmwareRoot 'mkisofs.exe'
$ova = Join-Path $VmRoot 'images\jammy-server-cloudimg-amd64.ova'
$passwordPath = Join-Path $VmRoot 'secrets\legosim-guest-password.txt'
$name = "legosim-node-$Slot-fresh"
$nodeDirectory = Join-Path $VmRoot "fresh-nodes\$name"
$vmxPath = Join-Path $nodeDirectory "$name.vmx"
$seedDirectory = Join-Path $nodeDirectory 'seed'
$seedIso = Join-Path $seedDirectory 'seed.iso'

foreach ($path in @($ovfTool, $vmrun, $mkisofs, $ova, $passwordPath)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required input is missing: $path" }
}
if (Test-Path -LiteralPath $vmxPath) { throw "Fresh node already exists: $vmxPath" }

New-Item -ItemType Directory -Force -Path $nodeDirectory, $seedDirectory | Out-Null
$password = (Get-Content -LiteralPath $passwordPath -Raw).Trim()
$utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
$userData = @"
#cloud-config
hostname: $name
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
packages: [docker.io, openssh-server, open-vm-tools]
runcmd:
  - systemctl enable --now docker
  - systemctl restart ssh
  - touch /var/lib/legosim-bootstrap-ready
"@
$metaData = "instance-id: $name`nlocal-hostname: $name`n"
[System.IO.File]::WriteAllText((Join-Path $seedDirectory 'user-data'), $userData, $utf8WithoutBom)
[System.IO.File]::WriteAllText((Join-Path $seedDirectory 'meta-data'), $metaData, $utf8WithoutBom)
Push-Location $seedDirectory
try {
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $mkisofs -o $seedIso -V cidata -r 'user-data' 'meta-data' *> $null
    $mkisofsExit = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorAction
}
finally { Pop-Location }
if ($mkisofsExit -ne 0) { throw 'Could not create node seed ISO' }

& $ovfTool --acceptAllEulas --overwrite --name=$name $ova $vmxPath
if ($LASTEXITCODE -ne 0) { throw 'OVF Tool import failed' }
$diskName = [System.IO.Path]::GetFileName((Get-ChildItem -LiteralPath $nodeDirectory -Filter '*-disk1.vmdk' -File | Select-Object -First 1).FullName)
if (-not $diskName) { throw 'Imported OVA did not create a boot VMDK' }
$seedIsoVmx = $seedIso.Replace('\', '/')
$vmx = @"
.encoding = "UTF-8"
config.version = "8"
virtualHW.version = "10"
displayName = "$name"
guestOS = "ubuntu-64"
firmware = "bios"
memsize = "$MemoryMiB"
numvcpus = "$Processors"
scsi0.present = "TRUE"
scsi0.virtualDev = "lsilogic"
scsi0:0.present = "TRUE"
scsi0:0.deviceType = "disk"
scsi0:0.fileName = "$diskName"
ethernet0.present = "TRUE"
ethernet0.connectionType = "nat"
ethernet0.vnet = "VMnet8"
ethernet0.virtualDev = "e1000"
ethernet0.addressType = "generated"
ethernet0.startConnected = "TRUE"
ide1:0.present = "TRUE"
ide1:0.deviceType = "cdrom-image"
ide1:0.fileName = "$seedIsoVmx"
ide1:0.startConnected = "TRUE"
tools.syncTime = "FALSE"
"@
[System.IO.File]::WriteAllText($vmxPath, $vmx, [System.Text.UTF8Encoding]::new($false))

& $vmrun -T ws start $vmxPath nogui
if ($LASTEXITCODE -ne 0) { throw 'Could not start fresh node' }
Write-Output "Fresh node created: $name"
Write-Output "VMX: $vmxPath"
Write-Output 'MAC: VMware-generated on first boot'
