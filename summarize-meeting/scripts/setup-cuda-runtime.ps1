param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeRoot = Join-Path $projectRoot "runtime\cuda"
$binDirectory = Join-Path $runtimeRoot "bin"
$downloadDirectory = Join-Path $runtimeRoot ".downloads"
$archivePath = Join-Path $downloadDirectory "cuBLAS.and.cuDNN_CUDA12_win_v2.7z"
$sevenZipPath = Join-Path $downloadDirectory "7zr.exe"

$archiveUrl = "https://github.com/Purfview/whisper-standalone-win/releases/download/libs/cuBLAS.and.cuDNN_CUDA12_win_v2.7z"
$archiveSha256 = "89D396373E2781E01FDD58D35A73AADF9B2DBA83D3DCD05A838B9115D50427C3"
$sevenZipUrl = "https://github.com/ip7z/7zip/releases/download/26.02/7zr.exe"
$sevenZipSha256 = "56B8CC9F4971CEF253644FAFE54063ED7FDCA551D4DEE0F8C6BAA81B855ACD72"

$requiredFiles = @(
    "cublas64_12.dll",
    "cublasLt64_12.dll",
    "cudnn64_9.dll",
    "cudnn_adv64_9.dll",
    "cudnn_cnn64_9.dll",
    "cudnn_engines_precompiled64_9.dll",
    "cudnn_engines_runtime_compiled64_9.dll",
    "cudnn_graph64_9.dll",
    "cudnn_heuristic64_9.dll",
    "cudnn_ops64_9.dll"
)

function Test-RuntimeComplete {
    foreach ($file in $requiredFiles) {
        $path = Join-Path $binDirectory $file
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            return $false
        }
        if ((Get-Item -LiteralPath $path).Length -le 0) {
            return $false
        }
    }
    return $true
}

function Get-VerifiedDownload {
    param(
        [Parameter(Mandatory)] [string]$Uri,
        [Parameter(Mandatory)] [string]$Destination,
        [Parameter(Mandatory)] [string]$ExpectedSha256
    )

    if (-not (Test-Path -LiteralPath $Destination -PathType Leaf) -or $Force) {
        Invoke-WebRequest -Uri $Uri -OutFile $Destination
    }
    $actual = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash
    if ($actual -ne $ExpectedSha256) {
        throw "SHA-256 mismatch: $Destination expected=$ExpectedSha256 actual=$actual"
    }
}

function Remove-DownloadCache {
    if (-not (Test-Path -LiteralPath $downloadDirectory)) {
        return
    }
    $resolvedRuntime = [System.IO.Path]::GetFullPath($runtimeRoot)
    $resolvedDownload = [System.IO.Path]::GetFullPath($downloadDirectory)
    $expectedPrefix = $resolvedRuntime.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolvedDownload.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Download cache escaped runtime directory: $resolvedDownload"
    }
    Remove-Item -LiteralPath $resolvedDownload -Recurse -Force
}

if ((Test-RuntimeComplete) -and -not $Force) {
    Remove-DownloadCache
    Write-Output "CUDA runtime is already available: $binDirectory"
    exit 0
}

New-Item -ItemType Directory -Path $binDirectory, $downloadDirectory -Force | Out-Null
Get-VerifiedDownload -Uri $archiveUrl -Destination $archivePath -ExpectedSha256 $archiveSha256
Get-VerifiedDownload -Uri $sevenZipUrl -Destination $sevenZipPath -ExpectedSha256 $sevenZipSha256

& $sevenZipPath x $archivePath "-o$binDirectory" -aoa
if ($LASTEXITCODE -ne 0) {
    throw "7-Zip extraction failed with exit code $LASTEXITCODE"
}
if (-not (Test-RuntimeComplete)) {
    throw "CUDA runtime extraction completed but required DLLs are missing"
}
Remove-DownloadCache

Write-Output "CUDA runtime is ready: $binDirectory"
Write-Output "Review NVIDIA and archive redistribution licenses before packaging these DLLs."
