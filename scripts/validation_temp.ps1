function Enter-FlussraValidationTemp {
    $originalVariables = [System.Environment]::GetEnvironmentVariables([System.EnvironmentVariableTarget]::Process)
    $state = [pscustomobject]@{
        HadTemp = $originalVariables.Contains("TEMP")
        HadTmp = $originalVariables.Contains("TMP")
        OriginalTemp = $originalVariables["TEMP"]
        OriginalTmp = $originalVariables["TMP"]
        Root = $null
        ProbePath = $null
    }

    if ($IsWindows -or $env:OS -eq "Windows_NT") {
        $state.Root = "C:\Temp"
    } else {
        $state.Root = [System.IO.Path]::GetTempPath()
    }

    try {
        if (-not (Test-Path -LiteralPath $state.Root -PathType Container)) {
            New-Item -ItemType Directory -Path $state.Root -Force | Out-Null
        }
        $env:TEMP = $state.Root
        $env:TMP = $state.Root

        $state.ProbePath = Join-Path $state.Root ("flussra-validation-probe-" + [guid]::NewGuid().ToString("N") + ".tmp")
        $probeBytes = [System.Text.Encoding]::UTF8.GetBytes("Flussra validation temp probe")
        try {
            $probeStream = [System.IO.File]::Open($state.ProbePath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
            try {
                $probeStream.Write($probeBytes, 0, $probeBytes.Length)
                $probeStream.Flush()
            } finally {
                $probeStream.Dispose()
            }
            $writtenBytes = [System.IO.File]::ReadAllBytes($state.ProbePath)
            if ([System.Convert]::ToBase64String($writtenBytes) -ne [System.Convert]::ToBase64String($probeBytes)) {
                throw "temporary write probe contents did not match"
            }
        } finally {
            if (Test-Path -LiteralPath $state.ProbePath -PathType Leaf) {
                Remove-Item -LiteralPath $state.ProbePath -Force
            }
        }
    } catch {
        $setupError = $_
        try {
            Exit-FlussraValidationTemp $state
        } catch {
            throw "Temporary setup failed: $($setupError.Exception.Message); environment restoration failed: $($_.Exception.Message)"
        }
        throw $setupError
    }

    return $state
}

function Exit-FlussraValidationTemp {
    param([Parameter(Mandatory = $true)]$State)

    $restoreErrors = @()
    try {
        if ($State.HadTemp) {
            [System.Environment]::SetEnvironmentVariable("TEMP", [string]$State.OriginalTemp, [System.EnvironmentVariableTarget]::Process)
        } else {
            Remove-Item Env:TEMP -ErrorAction Stop
        }
    } catch {
        $restoreErrors += "TEMP: $($_.Exception.Message)"
    }
    try {
        if ($State.HadTmp) {
            [System.Environment]::SetEnvironmentVariable("TMP", [string]$State.OriginalTmp, [System.EnvironmentVariableTarget]::Process)
        } else {
            Remove-Item Env:TMP -ErrorAction Stop
        }
    } catch {
        $restoreErrors += "TMP: $($_.Exception.Message)"
    }
    if ($restoreErrors.Count -gt 0) {
        throw "Could not restore caller environment: $($restoreErrors -join '; ')"
    }
}
