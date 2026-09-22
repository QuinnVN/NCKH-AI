[CmdletBinding()]
param(
    [string]$LlamaServerCommand = "llama-server",
    [string]$BindAddress = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 8080,
    [ValidateRange(8192, 65536)]
    [int]$ContextSize = 32768,
    [ValidateRange(1, 8)]
    [int]$ParallelSlots = 1,
    [ValidateRange(0, 999)]
    [int]$GpuLayers = 99,
    [string]$ModelSource = "Qwen/Qwen3-8B-GGUF:Q4_K_M",
    [string]$Alias = "qwen3-8b"
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($LlamaServerCommand)) {
    throw "LlamaServerCommand cannot be empty."
}

$serverCommand = Get-Command $LlamaServerCommand -ErrorAction SilentlyContinue
if ($null -eq $serverCommand) {
    throw "The '$LlamaServerCommand' command was not found on PATH."
}

$serverArguments = @(
    "-hf", $ModelSource,
    "--alias", $Alias,
    "--host", $BindAddress,
    "--port", $Port,
    "--ctx-size", $ContextSize,
    "--parallel", $ParallelSlots,
    "--jinja",
    "--reasoning", "auto",
    "--reasoning-format", "deepseek",
    "--reasoning-budget", "2048",
    "--no-context-shift",
    "-ngl", $GpuLayers
)

Write-Host "Starting Qwen3-8B as '$Alias' on http://${BindAddress}:$Port"
Write-Host "The first run may download the selected GGUF from Hugging Face."
$invocationArguments = $serverArguments
if ([System.IO.Path]::GetFileNameWithoutExtension($serverCommand.Source) -eq "llama") {
    $invocationArguments = @("serve") + $serverArguments
}
& $serverCommand.Source @invocationArguments
exit $LASTEXITCODE
