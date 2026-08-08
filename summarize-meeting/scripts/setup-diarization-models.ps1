param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$modelRoot = Join-Path $projectRoot "models\sherpa-onnx\diarization"
$segmentationDirectory = Join-Path $modelRoot "segmentation"
$embeddingDirectory = Join-Path $modelRoot "embedding"
$downloadDirectory = Join-Path $modelRoot ".downloads"
$extractDirectory = Join-Path $downloadDirectory "extract"
$segmentationArchive = Join-Path $downloadDirectory "segmentation.tar.bz2"
$embeddingDownload = Join-Path $downloadDirectory "nemo_en_titanet_small.onnx"

$segmentationUrl = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
$segmentationSha256 = "24615EE884C897D9D2BA09BB4D30DA6BB1B15E685065962DB5B02E76E4996488"
$embeddingUrl = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx"
$embeddingSha256 = "AD4A1802485D8B34C722D2A9D04249662F2ECE5D28A7A039063CA22F515A789E"

function Test-ModelsComplete {
    $required = @(
        (Join-Path $segmentationDirectory "model.int8.onnx"),
        (Join-Path $segmentationDirectory "LICENSE"),
        (Join-Path $segmentationDirectory "README.md"),
        (Join-Path $embeddingDirectory "nemo_en_titanet_small.onnx")
    )
    foreach ($path in $required) {
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

function Remove-WithinModelRoot {
    param([Parameter(Mandatory)] [string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $resolvedRoot = [System.IO.Path]::GetFullPath($modelRoot)
    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    $expectedPrefix = $resolvedRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolvedPath.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Cleanup target escaped model directory: $resolvedPath"
    }
    Remove-Item -LiteralPath $resolvedPath -Recurse -Force
}

function Copy-Atomic {
    param(
        [Parameter(Mandatory)] [string]$Source,
        [Parameter(Mandatory)] [string]$Destination
    )

    $temporary = "$Destination.tmp"
    Copy-Item -LiteralPath $Source -Destination $temporary -Force
    [System.IO.File]::Move($temporary, $Destination, $true)
}

if ((Test-ModelsComplete) -and -not $Force) {
    Remove-WithinModelRoot -Path $downloadDirectory
    Write-Output "Speaker diarization models are already available: $modelRoot"
    exit 0
}

New-Item -ItemType Directory -Path $downloadDirectory, $segmentationDirectory, $embeddingDirectory -Force | Out-Null
Get-VerifiedDownload -Uri $segmentationUrl -Destination $segmentationArchive -ExpectedSha256 $segmentationSha256
Get-VerifiedDownload -Uri $embeddingUrl -Destination $embeddingDownload -ExpectedSha256 $embeddingSha256

Remove-WithinModelRoot -Path $extractDirectory
New-Item -ItemType Directory -Path $extractDirectory -Force | Out-Null
& tar -xf $segmentationArchive -C $extractDirectory
if ($LASTEXITCODE -ne 0) {
    throw "Segmentation model extraction failed with exit code $LASTEXITCODE"
}
$extractedRoot = Join-Path $extractDirectory "sherpa-onnx-pyannote-segmentation-3-0"
$extractedModel = Join-Path $extractedRoot "model.int8.onnx"
if (-not (Test-Path -LiteralPath $extractedModel -PathType Leaf)) {
    throw "Segmentation archive does not contain model.int8.onnx"
}
Copy-Atomic -Source $extractedModel -Destination (Join-Path $segmentationDirectory "model.int8.onnx")
Copy-Atomic -Source (Join-Path $extractedRoot "LICENSE") -Destination (Join-Path $segmentationDirectory "LICENSE")
Copy-Atomic -Source (Join-Path $extractedRoot "README.md") -Destination (Join-Path $segmentationDirectory "README.md")
Copy-Atomic -Source $embeddingDownload -Destination (Join-Path $embeddingDirectory "nemo_en_titanet_small.onnx")

if (-not (Test-ModelsComplete)) {
    throw "Model setup completed but required files are missing"
}
Remove-WithinModelRoot -Path $downloadDirectory

Write-Output "Speaker diarization models are ready: $modelRoot"
