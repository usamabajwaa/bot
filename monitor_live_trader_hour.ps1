# Monitor live trader for 1 hour
$endTime = (Get-Date).AddHours(1)
$logFile = "live_trading.log"
$checkInterval = 30 # seconds

Write-Host "=" * 70
Write-Host "MONITORING LIVE TRADER FOR 1 HOUR"
Write-Host "Start Time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host "End Time: $($endTime.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Host "=" * 70
Write-Host ""

$issues = @()

while ((Get-Date) -lt $endTime) {
    $timeLeft = $endTime - (Get-Date)
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Time remaining: $([math]::Round($timeLeft.TotalMinutes, 1)) minutes"
    
    # Check if process is running
    $process = Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*live_trader.py*" -or $_.Path -like "*python*" } | Select-Object -First 1
    if (-not $process) {
        Write-Host "  WARNING: Python process not found!"
        $issues += "Process died at $(Get-Date)"
    } else {
        Write-Host "  Process running (PID: $($process.Id))"
    }
    
    # Check recent log entries for errors
    if (Test-Path $logFile) {
        $recentErrors = Get-Content $logFile -Tail 50 | Select-String -Pattern "ERROR|CRITICAL|Failed|failed" | Select-Object -Last 5
        if ($recentErrors) {
            Write-Host "  Recent errors/warnings:"
            $recentErrors | ForEach-Object { Write-Host "    $_" }
            $issues += "Errors found at $(Get-Date): $($recentErrors -join '; ')"
        }
        
        # Check for order placement issues
        $orderIssues = Get-Content $logFile -Tail 50 | Select-String -Pattern "order.*fail|missing.*SL|missing.*TP|without.*SL|without.*TP" -CaseSensitive:$false | Select-Object -Last 3
        if ($orderIssues) {
            Write-Host "  Order placement issues:"
            $orderIssues | ForEach-Object { Write-Host "    $_" }
            $issues += "Order issues at $(Get-Date): $($orderIssues -join '; ')"
        }
        
        # Check for successful orders
        $successfulOrders = Get-Content $logFile -Tail 50 | Select-String -Pattern "order.*placed.*verified|Stop loss.*placed|Take profit.*placed" -CaseSensitive:$false | Select-Object -Last 3
        if ($successfulOrders) {
            Write-Host "  Recent successful orders:"
            $successfulOrders | ForEach-Object { Write-Host "    $_" }
        }
    } else {
        Write-Host "  Log file not found"
    }
    
    Write-Host ""
    Start-Sleep -Seconds $checkInterval
}

Write-Host ""
Write-Host "=" * 70
Write-Host "MONITORING COMPLETE"
Write-Host "End Time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host "=" * 70

if ($issues.Count -gt 0) {
    Write-Host ""
    Write-Host "ISSUES FOUND:"
    $issues | ForEach-Object { Write-Host "  $_" }
} else {
    Write-Host ""
    Write-Host "No major issues detected during monitoring period."
}

# Final log summary
if (Test-Path $logFile) {
    Write-Host ""
    Write-Host "LAST 20 LOG LINES:"
    Get-Content $logFile -Tail 20
}



