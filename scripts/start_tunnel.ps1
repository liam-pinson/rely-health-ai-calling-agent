<#
.SYNOPSIS
  Starts a cloudflared quick tunnel pointed at the local backend
  (http://localhost:8000) and prints the generated
  https://*.trycloudflare.com URL, formatted for pasting into Retell's
  webhook config.

.DESCRIPTION
  This only starts the tunnel and shows you the URL. Pasting that URL into
  the Retell dashboard's webhook field is a manual step you still have to
  do yourself -- that requires your own Retell account login, which no
  script can do on your behalf.

  Keeps running in the foreground so the tunnel stays alive for your dev
  session. Press Ctrl+C to stop it (this also stops the underlying
  cloudflared process, so you don't end up with an orphaned tunnel).

.NOTES
  Requires cloudflared to be installed and on PATH. See:
  https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
#>

$ErrorActionPreference = "Stop"

$BackendUrl = "http://localhost:8000"
$CloudflaredBin = if ($env:CLOUDFLARED_BIN) { $env:CLOUDFLARED_BIN } else { "cloudflared" }
$LogFile = Join-Path $env:TEMP "ai_calling_agent_tunnel.out.log"
$ErrFile = Join-Path $env:TEMP "ai_calling_agent_tunnel.err.log"
Remove-Item -Path $LogFile, $ErrFile -ErrorAction SilentlyContinue

Write-Output "Starting cloudflared tunnel -> $BackendUrl ..."

try {
    $proc = Start-Process -FilePath $CloudflaredBin `
        -ArgumentList @("tunnel", "--url", $BackendUrl) `
        -NoNewWindow -PassThru `
        -RedirectStandardOutput $LogFile `
        -RedirectStandardError $ErrFile
}
catch {
    Write-Error "Couldn't start '$CloudflaredBin'. Is cloudflared installed and on PATH? $($_.Exception.Message)"
    exit 1
}

# cloudflared prints the tunnel URL banner to stderr, not stdout -- check both.
function Get-TunnelUrl {
    foreach ($f in @($ErrFile, $LogFile)) {
        if (Test-Path $f) {
            $match = Select-String -Path $f -Pattern 'https://[a-zA-Z0-9\-]+\.trycloudflare\.com' -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($match) { return $match.Matches[0].Value }
        }
    }
    return $null
}

$tunnelUrl = $null
$timeoutSeconds = 30
$elapsed = 0.0
while (-not $tunnelUrl -and $elapsed -lt $timeoutSeconds) {
    Start-Sleep -Milliseconds 500
    $elapsed += 0.5
    if ($proc.HasExited) {
        Write-Error "cloudflared exited unexpectedly (exit code $($proc.ExitCode)). Check $ErrFile for details."
        exit 1
    }
    $tunnelUrl = Get-TunnelUrl
}

if (-not $tunnelUrl) {
    Write-Error "Timed out after $timeoutSeconds seconds waiting for cloudflared to print a tunnel URL. Check $ErrFile."
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

$webhookUrl = "$tunnelUrl/events"

Write-Output ""
Write-Output "============================================"
Write-Output "Webhook tunnel is up. Paste this into the"
Write-Output "Retell dashboard's webhook URL field:"
Write-Output ""
Write-Output $webhookUrl
Write-Output "============================================"
Write-Output ""
Write-Output "NOTE: pasting this URL into the Retell dashboard is a manual step -- this"
Write-Output "script cannot do it for you (that requires your own Retell account login)."
Write-Output "A fresh URL is generated every time this script (re)starts, so re-paste"
Write-Output "it into the dashboard whenever you restart the tunnel."
Write-Output ""
Write-Output "Tunnel is running in the foreground (PID $($proc.Id)). Press Ctrl+C to stop it."
Write-Output ""

try {
    Wait-Process -Id $proc.Id
}
finally {
    if (-not $proc.HasExited) {
        Write-Output "Stopping tunnel (PID $($proc.Id))..."
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
}