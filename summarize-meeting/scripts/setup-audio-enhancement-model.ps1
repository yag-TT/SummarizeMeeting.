param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$modelDirectory = Join-Path $projectRoot "models\sherpa-onnx\speech-enhancement"
$modelPath = Join-Path $modelDirectory "dpdfnet2_48khz_hr.onnx"
$downloadPath = Join-Path $modelDirectory ".dpdfnet2_48khz_hr.onnx.download"
$modelUrl = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speech-enhancement-models/dpdfnet2_48khz_hr.onnx"
$modelSha256 = "0B399F8A58DC4D70D8CD97541F5C39869406145193B957D00A03B66070944928"

function Test-Model {
    if (-not (Test-Path -LiteralPath $modelPath -PathType Leaf)) {
        return $false
    }
    return (Get-FileHash -LiteralPath $modelPath -Algorithm SHA256).Hash -eq $modelSha256
}

if ((Test-Model) -and -not $Force) {
    Write-Output "Audio enhancement model is already available: $modelPath"
    exit 0
}

New-Item -ItemType Directory -Path $modelDirectory -Force | Out-Null
try {
    Invoke-WebRequest -Uri $modelUrl -OutFile $downloadPath
    $actualSha256 = (Get-FileHash -LiteralPath $downloadPath -Algorithm SHA256).Hash
    if ($actualSha256 -ne $modelSha256) {
        throw "SHA-256 mismatch: expected=$modelSha256 actual=$actualSha256"
    }
    [System.IO.File]::Move($downloadPath, $modelPath, $true)
}
finally {
    Remove-Item -LiteralPath $downloadPath -Force -ErrorAction SilentlyContinue
}

if (-not (Test-Model)) {
    throw "Model setup completed but the model is invalid: $modelPath"
}
Write-Output "Audio enhancement model is ready: $modelPath"
