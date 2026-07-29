<##
.SYNOPSIS
Build independently auditable Docker images for complete single-stage benchmarks.

.DESCRIPTION
Each image contains exactly one benchmark directory copied from the external
single_stage_simulator worktree.  A JSON report records both successful images
and build failures without treating a failed build as a runnable workload.
##>
[CmdletBinding()]
param(
    [string[]]$Benchmarks = @(
        'matmul', 'BFS', 'FFT', 'Pagerank', 'PDE', 'MLP', 'resnet', 'dlrm_cpp_large'
    ),
    [string]$BaseImage = 'legosim-real:8166851-simbricks-integrated',
    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$workspaceRoot = (Resolve-Path (Join-Path $repoRoot '..')).Path
$sourceRoot = Join-Path $workspaceRoot 'single_stage_simulator\benchmark'
$dockerfile = Join-Path $repoRoot 'docker\Dockerfile.single_stage_benchmark'
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repoRoot 'results\single-stage-builds'
}

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$report = @()

foreach ($benchmark in $Benchmarks) {
    $sourceDirectory = Join-Path $sourceRoot $benchmark
    $yamlFiles = @(Get-ChildItem -Path $sourceDirectory -Filter '*.yml' -File -ErrorAction SilentlyContinue)
    $complete = (Test-Path (Join-Path $sourceDirectory 'makefile')) -and $yamlFiles.Count -gt 0
    $image = "legosim-real:single-stage-$($benchmark.ToLower())"
    $logPath = Join-Path $OutputDirectory "$benchmark.build.log"
    $entry = [ordered]@{
        benchmark = $benchmark
        complete_source = $complete
        yaml_files = @($yamlFiles.Name)
        image = $image
        started_utc = (Get-Date).ToUniversalTime().ToString('o')
        status = 'skipped'
        exit_code = $null
        log = $logPath
    }

    if (-not $complete) {
        $entry.status = 'incomplete-source'
        $report += [pscustomobject]$entry
        continue
    }

    # BuildKit writes progress to stderr even on success.  Do not let
    # $ErrorActionPreference convert that native-program output into a
    # terminating PowerShell error; retain Docker's own exit code below.
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & docker build --progress=plain -f $dockerfile `
        --build-arg "BASE_IMAGE=$BaseImage" `
        --build-arg "BENCHMARK=$benchmark" `
        -t $image $workspaceRoot 2>&1 | Tee-Object -FilePath $logPath
    $dockerExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorAction
    $entry.exit_code = $dockerExitCode
    $entry.status = if ($dockerExitCode -eq 0) { 'built' } else { 'build-failed' }
    $entry.finished_utc = (Get-Date).ToUniversalTime().ToString('o')
    $report += [pscustomobject]$entry
    $report | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 (Join-Path $OutputDirectory 'build-summary.json')
}

$report | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 (Join-Path $OutputDirectory 'build-summary.json')
if ($report.status -contains 'build-failed') { exit 1 }
