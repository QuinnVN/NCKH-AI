[CmdletBinding()]
param(
    [string]$LlamaServerCommand = "llama-server",
    [string]$BindAddress = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 8080,
    [ValidateRange(4096, 32768)]
    [int]$ContextSize = 16384,
    [ValidateRange(0, 999)]
    [int]$GpuLayers = 99,
    [string]$ModelSource = "Qwen/Qwen3-4B-GGUF:Q4_K_M",
    [string]$Alias = "qwen3-4b"
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
    "--jinja",
    "--reasoning", "auto",
    "--reasoning-format", "deepseek",
    "--reasoning-budget", "1024",
    "--no-context-shift",
    "-ngl", $GpuLayers
)

Write-Host "Starting Qwen3-4B as '$Alias' on http://${BindAddress}:$Port"
Write-Host "The first run may download the selected GGUF from Hugging Face."
& $serverCommand.Source @serverArguments
exit $LASTEXITCODE
