$ErrorActionPreference = 'Stop'

$axonicBackendRoot = Split-Path -Parent $PSScriptRoot
$axonicFrontendRoot = Join-Path (Split-Path -Parent $axonicBackendRoot) 'Axonic-app'
$axonicPython = Join-Path $axonicBackendRoot '.venv\Scripts\python.exe'
$axonicTypeScript = Join-Path $axonicFrontendRoot 'node_modules\typescript\bin\tsc'
$axonicMobileTestsRoot = Join-Path $axonicFrontendRoot 'tests'
$axonicServiceCycleCheck = Join-Path $axonicFrontendRoot 'scripts\check-service-cycles.cjs'

if (-not (Test-Path -LiteralPath $axonicPython)) {
    throw "Backend virtual-environment Python was not found at $axonicPython"
}

if (-not (Test-Path -LiteralPath $axonicTypeScript)) {
    throw "Frontend TypeScript compiler was not found at $axonicTypeScript"
}

if (-not (Test-Path -LiteralPath $axonicMobileTestsRoot -PathType Container)) {
    throw "Frontend unit-test directory was not found at $axonicMobileTestsRoot"
}

$axonicMobileUnitTests = @(
    Get-ChildItem -LiteralPath $axonicMobileTestsRoot -Filter '*.test.cjs' -File |
        Sort-Object -Property FullName
)
if ($axonicMobileUnitTests.Count -eq 0) {
    throw "No frontend unit tests were found in $axonicMobileTestsRoot"
}

if (-not (Test-Path -LiteralPath $axonicServiceCycleCheck)) {
    throw "Frontend service-cycle check was not found at $axonicServiceCycleCheck"
}

Write-Host 'Checking the Django configuration...'
Push-Location $axonicBackendRoot
try {
    & $axonicPython manage.py check --settings=config.settings_test
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host 'Checking for missing Django migrations...'
    & $axonicPython manage.py makemigrations --check --dry-run --settings=config.settings_test
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host 'Running backend tests...'
    & $axonicPython manage.py test --settings=config.settings_test
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    Pop-Location
}

Write-Host 'Type-checking the mobile app...'
Push-Location $axonicFrontendRoot
try {
    & node $axonicTypeScript --noEmit
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host 'Running mobile reliability tests...'
    & node --test $axonicMobileUnitTests.FullName
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host 'Checking mobile service dependencies...'
    & node $axonicServiceCycleCheck
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    Pop-Location
}

Write-Host 'Axonic verification passed.'
