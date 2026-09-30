$ErrorActionPreference = 'Stop'
Set-Location 'C:\Users\tsahn\Documents\GitHub\memecoin_trading'

$patchDir = 'C:\Users\tsahn\Documents\Codex\2026-09-28\github-plugin-github-openai-curated-remote-2\outputs'

# Keep your existing session key. Create or replace it only if missing or too short.
$envPath = Join-Path (Get-Location) '.env'
if (-not (Test-Path -LiteralPath $envPath)) {
    throw 'Create .env from .env.example and set your existing database and Google sign-in values first.'
}
$envText = [System.IO.File]::ReadAllText($envPath)
$keyMatch = [regex]::Match($envText, '(?m)^SESSION_SECRET_KEY=(.*)$')
if (-not $keyMatch.Success -or $keyMatch.Groups[1].Value.Trim().Length -lt 48) {
    $bytes = New-Object byte[] 24
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $newKey = -join ($bytes | ForEach-Object { $_.ToString('x2') })
    $rng.Dispose()

    if ($keyMatch.Success) {
        $envText = [regex]::Replace($envText, '(?m)^SESSION_SECRET_KEY=.*$', "SESSION_SECRET_KEY=$newKey")
    } else {
        $envText = $envText.TrimEnd() + "`r`nSESSION_SECRET_KEY=$newKey`r`n"
    }
    [System.IO.File]::WriteAllText(
        $envPath,
        $envText,
        (New-Object System.Text.UTF8Encoding($false))
    )
}

$patches = @(
    'compose_migration_gate.patch',
    'production_readiness_auth_risk.patch',
    'position_pool_pinning.patch',
    'daily_loss_budget.patch',
    'session_secret_required.patch',
    'legacy_pool_quarantine.patch'
)

foreach ($name in $patches) {
    $patch = Join-Path $patchDir $name
    if (-not (Test-Path -LiteralPath $patch)) {
        throw "Patch file not found: $patch"
    }

    & git apply --check $patch
    if ($LASTEXITCODE -ne 0) {
        throw "Patch check failed for $name. No part of that patch was applied; stop here and send me the error."
    }

    & git apply $patch
    if ($LASTEXITCODE -ne 0) {
        throw "Patch application failed for $name; stop here and send me the error."
    }

    Write-Host "Applied $name"
}

& git diff --check
if ($LASTEXITCODE -ne 0) {
    throw 'Whitespace check failed; stop here and send me the output.'
}

docker compose config --quiet
if ($LASTEXITCODE -ne 0) {
    throw 'Docker Compose configuration check failed.'
}

docker compose up -d --build --force-recreate migrate web
if ($LASTEXITCODE -ne 0) {
    throw 'Docker Compose build/start failed.'
}

docker compose ps
docker compose logs --since 5m migrate web