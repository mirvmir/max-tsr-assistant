param(
    [ValidateSet('start', 'stop', 'status', 'logs', 'demo')]
    [string]$Action = 'start',
    [switch]$Offline
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "Docker failed (exit $LASTEXITCODE)." }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Install Docker Desktop, then run this script again.'
}

$composeFiles = @('-f', 'compose.yaml', '-f', 'compose.polling.yaml')
if (Test-Path -LiteralPath 'compose.local-ca.yaml') {
    $composeFiles += @('-f', 'compose.local-ca.yaml')
}

switch ($Action) {
    'start' {
        & docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
            if (-not (Test-Path -LiteralPath $desktop)) { throw 'Start Docker Desktop first.' }
            Start-Process -FilePath $desktop -WindowStyle Hidden
            $deadline = (Get-Date).AddSeconds(90)
            do {
                Start-Sleep -Seconds 3
                & docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
                if ($LASTEXITCODE -eq 0) { break }
            } while ((Get-Date) -lt $deadline)
            if ($LASTEXITCODE -ne 0) { throw 'Docker Desktop is not ready. Check its status.' }
        }
        if (-not (Test-Path -LiteralPath '.env')) {
            Copy-Item -LiteralPath '.env.example' -Destination '.env'
        }
        $previousPythonPath = $env:PYTHONPATH
        try {
            $env:PYTHONPATH = 'src'
            & python -c "from pathlib import Path; from tsr.bootstrap import generate_demo_secrets; generate_demo_secrets(Path('secrets'))"
            if ($LASTEXITCODE -ne 0) { throw 'Could not initialize secrets. Python 3.12+ is required.' }
        } finally {
            $env:PYTHONPATH = $previousPythonPath
        }
        $tokenPath = Join-Path $PSScriptRoot 'secrets\max_bot_token'
        $hasToken = -not [string]::IsNullOrWhiteSpace([IO.File]::ReadAllText($tokenPath))
        # Build the shared image once, then recreate processes to reload secrets.
        Invoke-Docker compose @composeFiles build app
        $services = @('db', 'app', 'worker')
        if (-not $Offline -and $hasToken) {
            $services += 'polling'
        } else {
            Invoke-Docker compose @composeFiles stop polling
        }
        Invoke-Docker compose @composeFiles up -d --no-build --force-recreate @services
        Invoke-Docker compose @composeFiles ps
        Write-Host 'Local server: http://127.0.0.1:8080/health/live'
        if ($Offline -or -not $hasToken) {
            Write-Host 'Local demo is available. For MAX, save the token to secrets\max_bot_token and run .\start.ps1.'
        } else {
            Write-Host 'Check MAX connection: .\start.ps1 logs. Then open your bot in MAX and press Start.'
        }
    }
    'stop' { Invoke-Docker compose @composeFiles stop }
    'status' { Invoke-Docker compose @composeFiles ps }
    'logs' { Invoke-Docker compose @composeFiles logs --no-color --tail 60 app worker polling }
    'demo' {
        Invoke-Docker compose @composeFiles exec -T app tsr demo --dataset public --scenario both --download-dir /tmp/tsr-demo
        New-Item -ItemType Directory -Path 'var\demo-downloads' -Force | Out-Null
        Invoke-Docker compose @composeFiles cp 'app:/tmp/tsr-demo/.' 'var/demo-downloads/'
        Write-Host ('Documents: ' + (Join-Path $PSScriptRoot 'var\demo-downloads'))
    }
}
