# Деплой: GitHub + напоминание про Railway Variables
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
        [System.Environment]::GetEnvironmentVariable("Path", "User")
}

gh auth status 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Войдите в GitHub (откроется браузер)..."
    gh auth login --hostname github.com --git-protocol https --web
}

if (-not (git remote get-url origin 2>$null)) {
    git remote add origin https://github.com/vomitboy2007/tg_bot.git
}

$exists = gh repo view vomitboy2007/tg_bot 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Создаю репозиторий vomitboy2007/tg_bot ..."
    gh repo create vomitboy2007/tg_bot --public --source=. --remote=origin --push
} else {
    git push -u origin main
}

Write-Host ""
Write-Host "GitHub готов. В Railway добавьте Variables:"
Write-Host "  TELEGRAM_TOKEN, OPENAI_API_KEY, CONTEXT_WINDOW=15"
Write-Host "  https://railway.app -> New Project -> Deploy from GitHub -> tg_bot"
