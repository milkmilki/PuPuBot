$ErrorActionPreference = "Stop"

$repoRoot = (git rev-parse --show-toplevel).Trim()
$hookDir = Join-Path $repoRoot ".git/hooks"
$hookPath = Join-Path $hookDir "pre-commit"
$scriptPath = Join-Path $repoRoot "scripts/check_branch_scope.py"

New-Item -ItemType Directory -Force -Path $hookDir | Out-Null

$hookContent = @"
#!/bin/sh
python "scripts/check_branch_scope.py"
"@
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($hookPath, $hookContent.Replace("`r`n", "`n"), $utf8NoBom)

Write-Host "Installed pre-commit branch scope hook:"
Write-Host "  $hookPath"
Write-Host "It blocks Siri UI files on main and backend/runtime files on siri."
