[CmdletBinding()]
param(
    [ValidateRange(1, 8)]
    [int]$NodeCount = 2,

    [string]$SourceImage = 'legosim-real:8166851-simbricks-pthread-bfs-compat',

    [string]$Repository = 'legosim-real',

    [string]$Tag = 'bfs-compat',

    [string]$RegistryContainer = 'legosim-registry',

    [string]$DindPrefix = 'legosim-dind-worker'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-Docker {
    param([Parameter(Mandatory)][string[]]$Arguments)

    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Get-NodeIdByHostname {
    param([Parameter(Mandatory)][string]$Hostname)

    $line = (& docker node ls --format '{{.ID}} {{.Hostname}}' |
        Where-Object { $_ -match "^([^ ]+)\s+$([regex]::Escape($Hostname))$" } |
        Select-Object -First 1)
    if (-not $line) {
        return $null
    }
    return ($line -split '\s+')[0]
}

function Wait-DindDaemon {
    param([Parameter(Mandatory)][string]$Container)

    for ($attempt = 0; $attempt -lt 30; ++$attempt) {
        & docker exec $Container docker info --format '{{.ServerVersion}}' *> $null
        if ($LASTEXITCODE -eq 0) {
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "DIND daemon in $Container did not become ready"
}

$managerAddress = (& docker node inspect self --format '{{.ManagerStatus.Addr}}').Trim()
if (-not $managerAddress) {
    throw 'The current Docker daemon is not an active Swarm manager.'
}

$registryId = (& docker ps -aq --filter "name=^$RegistryContainer$").Trim()
if (-not $registryId) {
    Invoke-Docker @('run', '-d', '--name', $RegistryContainer, '-p', '5000:5000', 'registry:2')
}

# Docker Desktop exposes the registry to its manager at localhost and to each
# nested daemon at host.docker.internal. Both tags denote the same manifest.
$managerImage = "localhost:5000/$Repository`:$Tag"
$workerImage = "host.docker.internal:5000/$Repository`:$Tag"
Invoke-Docker @('tag', $SourceImage, $managerImage)
Invoke-Docker @('push', $managerImage)
Invoke-Docker @('tag', $managerImage, $workerImage)

$joinToken = (& docker swarm join-token -q worker).Trim()
if (-not $joinToken) {
    throw 'Could not obtain a Swarm worker join token.'
}

for ($slot = 1; $slot -lt $NodeCount; ++$slot) {
    $container = "$DindPrefix-$slot"
    $containerId = (& docker ps -aq --filter "name=^$container$").Trim()
    if (-not $containerId) {
        Invoke-Docker @(
            'run', '-d', '--name', $container, '--privileged',
            'docker:27-dind', '--insecure-registry=host.docker.internal:5000'
        )
        $containerId = (& docker ps -aq --filter "name=^$container$").Trim()
    }

    Wait-DindDaemon -Container $container
    $hostname = $containerId.Substring(0, 12)
    $nodeId = Get-NodeIdByHostname -Hostname $hostname
    if (-not $nodeId) {
        Invoke-Docker @('exec', $container, 'docker', 'swarm', 'join', '--token', $joinToken, $managerAddress)
        for ($attempt = 0; $attempt -lt 30 -and -not $nodeId; ++$attempt) {
            Start-Sleep -Seconds 2
            $nodeId = Get-NodeIdByHostname -Hostname $hostname
        }
    }
    if (-not $nodeId) {
        throw "Worker $container joined but is not visible to the manager."
    }

    Invoke-Docker @('node', 'update', '--label-add', "legosim.node.$slot=true", '--label-add', 'legosim.dind=true', $nodeId)
    Invoke-Docker @('exec', $container, 'docker', 'pull', $workerImage)
}

Write-Host "Provisioned $NodeCount Swarm nodes. Use this image in stack generation: $workerImage"
& docker node ls
