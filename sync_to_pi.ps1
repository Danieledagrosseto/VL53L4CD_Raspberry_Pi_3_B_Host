<#
.SYNOPSIS
Sync this project from Windows to a Raspberry Pi over SSH/SCP.

.DESCRIPTION
Creates a temporary tar archive of the project (excluding local development
artifacts), uploads it to the Pi, extracts it in the target folder, and fixes
permissions so follow-up writes (for example virtualenv creation) do not fail.

Optional switches allow dependency installation into a project-local virtual
environment and immediate execution of main.py on the Pi.

.EXAMPLE
.\sync_to_pi.ps1 -PiHost 192.168.1.7 -PiUser daniele -InstallRequirements -RunMain
#>
param(
    [string]$PiHost = "raspberrypi.local",
    [string]$PiUser = "pi",
    [string]$TargetDir = "~/VL53L4CD_Raspberry_Pi_3_B+_Host",
    [string]$VenvDir = ".venv",
    [switch]$InstallRequirements,
    [switch]$RunMain
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$excludeNames = @(".git", ".venv", ".vscode", "__pycache__")
$syncId = [Guid]::NewGuid().ToString("N")

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList,

        [Parameter(Mandatory = $true)]
        [string]$ErrorMessage
    )

    # Centralized external command runner: keeps exit-code handling consistent
    # for ssh/scp/tar and gives a clear high-level failure message.
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$ErrorMessage (exit code $LASTEXITCODE)"
    }
}

Write-Host "Syncing project from: $projectRoot"
Write-Host "Destination: $PiUser@$PiHost`:$TargetDir"

Invoke-External -FilePath "ssh" -ArgumentList @("$PiUser@$PiHost", "mkdir -p $TargetDir && chmod u+rwx $TargetDir") -ErrorMessage "Failed to prepare destination directory on Pi"

# Use a unique archive name per sync to avoid collisions when multiple shells
# run this script in parallel.
$archivePath = Join-Path $env:TEMP ("vl53l4cd-sync-" + $syncId + ".tar")
$remoteArchivePath = "/tmp/.sync_bundle_$syncId.tar"

try {
    # Build a single archive on Windows first. Compared to recursive file copy,
    # this reduces SSH prompts and avoids leaving a half-copied tree if transfer
    # fails midway.
    $tarArgs = @("-cf", $archivePath)
    foreach ($exclude in $excludeNames) {
        $tarArgs += @("--exclude=$exclude")
    }
    $tarArgs += @("-C", $projectRoot, ".")

    Invoke-External -FilePath "tar" -ArgumentList $tarArgs -ErrorMessage "Failed to create project archive"

    # Upload archive to /tmp then extract in target directory.
    # /tmp avoids polluting project path with intermediate sync artifacts.
    Invoke-External -FilePath "scp" -ArgumentList @($archivePath, "$PiUser@$PiHost`:$remoteArchivePath") -ErrorMessage "Failed to transfer archive to Pi"

    Invoke-External -FilePath "ssh" -ArgumentList @("$PiUser@$PiHost", "tar -xf /tmp/.sync_bundle_$syncId.tar -C $TargetDir && rm -f /tmp/.sync_bundle_$syncId.tar") -ErrorMessage "Failed to extract archive on Pi"
    # Normalize ownership/write bits after extraction to prevent permission
    # errors when creating/updating the virtual environment.
    Invoke-External -FilePath "ssh" -ArgumentList @("$PiUser@$PiHost", "chmod -R u+rwX $TargetDir") -ErrorMessage "Failed to set write permissions on Pi target directory"

    if ($InstallRequirements) {
        Write-Host "Installing/updating Python dependencies in Pi virtual environment..."
        # Install into project-local venv instead of system site-packages.
        # This keeps deployment reproducible and avoids system package policies.
        $venvInstallCmd = "cd $TargetDir && python3 -m venv $VenvDir && $VenvDir/bin/python -m pip install --upgrade pip && $VenvDir/bin/pip install -r requirements.txt"
        Invoke-External -FilePath "ssh" -ArgumentList @("$PiUser@$PiHost", $venvInstallCmd) -ErrorMessage "Failed to install requirements in Pi virtual environment"
    }

    if ($RunMain) {
        Write-Host "Starting main.py on Pi..."
        # Prefer venv Python when present; fall back to system python3 so the
        # script still works when -InstallRequirements was not requested.
        $runCmd = "cd $TargetDir && if [ -x $VenvDir/bin/python ]; then $VenvDir/bin/python main.py; else python3 main.py; fi"
        Invoke-External -FilePath "ssh" -ArgumentList @("-t", "$PiUser@$PiHost", $runCmd) -ErrorMessage "Failed to start main.py on Pi"
    }
}
finally {
    # Always remove the local temporary archive, even if upload/extract failed.
    if (Test-Path -LiteralPath $archivePath) {
        Remove-Item -LiteralPath $archivePath -Force
    }
}

Write-Host "Sync complete."
