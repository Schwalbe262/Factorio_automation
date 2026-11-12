# Run before every commit: lint, then the full v2 test suite.
# Usage: powershell -ExecutionPolicy Bypass -File tools/check.ps1 [test_v2_module ...]
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    $env:PYTHONPATH = 'src;tests;runtime/v2/lua-test-deps'
    $files = git ls-files 'src/*.py' 'tests/*.py' 'tools/*.py'
    python -m ruff check -q $files
    if ($LASTEXITCODE -ne 0) { throw 'ruff check failed' }
    python -m ruff format -q --check $files
    if ($LASTEXITCODE -ne 0) { throw 'ruff format --check failed (run: python -m ruff format <file>)' }
    if ($args.Count -gt 0) { python -B -m unittest @args }
    else { python -B -m unittest discover -s tests -p 'test_v2*.py' }
    if ($LASTEXITCODE -ne 0) { throw 'tests failed' }
    'check passed'
}
finally { Pop-Location }
