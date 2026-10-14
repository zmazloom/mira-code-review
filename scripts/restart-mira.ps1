# Reload the persisted Windows token even when this terminal predates it.
param([switch]$Build, [switch]$Offline)

$ErrorActionPreference = 'Stop'
$reviewToken = [Environment]::GetEnvironmentVariable('GITHUB_TOKEN', 'User')
if (-not $reviewToken) {
    $reviewToken = [Environment]::GetEnvironmentVariable('GITHUB_TOKEN', 'Machine')
}
if (-not $reviewToken) {
    $reviewToken = $env:GITHUB_TOKEN
}
if (-not $reviewToken) {
    throw 'Set the Windows GITHUB_TOKEN environment variable before restarting Mira.'
}

$env:GITHUB_TOKEN = $reviewToken
foreach ($keyName in @('OPENROUTER_API_KEY', 'COMPANY_LLM_API_KEY')) {
    $savedKey = [Environment]::GetEnvironmentVariable($keyName, 'User')
    if (-not $savedKey) {
        $savedKey = [Environment]::GetEnvironmentVariable($keyName, 'Machine')
    }
    if ($savedKey) {
        Set-Item -LiteralPath "Env:$keyName" -Value $savedKey
    }
}
$projectDirectory = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectDirectory
try {
    $composeArguments = @('compose')
    if ($Offline) {
        $composeArguments += @('-f', 'compose.yaml', '-f', 'compose.offline.yaml')
    }
    $composeArguments += @('up', '-d', '--force-recreate')
    if ($Build) {
        $composeArguments += '--build'
    }
    docker @composeArguments
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker Compose could not restart Mira.'
    }
    docker compose exec -T mira python -c "import os,sys; configured=bool(os.getenv('GITHUB_TOKEN')); print('GITHUB_TOKEN: ' + ('Configured' if configured else 'Missing')); sys.exit(0 if configured else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw 'Mira did not receive GITHUB_TOKEN. Check the Compose environment mapping.'
    }
} finally {
    Pop-Location
}
