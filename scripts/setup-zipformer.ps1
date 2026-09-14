$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repositoryRoot
try {
    python -m app.sherpa_setup
}
finally {
    Pop-Location
}
