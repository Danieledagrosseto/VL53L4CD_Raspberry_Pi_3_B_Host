param(
dir
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

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$ErrorMessage (exit code $LASTEXITCODE)"
    }
}

Write-Host "Syncing project from: $projectRoot"
Write-Host "Destination: $PiUser@$PiHost`:$TargetDir"

Invoke-External -FilePath "ssh" -ArgumentList @("$PiUser@$PiHost", "mkdir -p $TargetDir && chmod u+rwx $TargetDir") -ErrorMessage "Failed to prepare destination directory on Pi"

$archivePath = Join-Path $env:TEMP ("vl53l4cd-sync-" + $syncId + ".tar")
$remoteArchivePath = "/tmp/.sync_bundle_$syncId.tar"

try {
    # Build one archive to avoid repeated password prompts and partial copies.
    $tarArgs = @("-cf", $archivePath)
    foreach ($exclude in $excludeNames) {
        $tarArgs += @("--exclude=$exclude")
    }
    $tarArgs += @("-C", $projectRoot, ".")

    Invoke-External -FilePath "tar" -ArgumentList $tarArgs -ErrorMessage "Failed to create project archive"

    Invoke-External -FilePath "scp" -ArgumentList @($archivePath, "$PiUser@$PiHost`:$remoteArchivePath") -ErrorMessage "Failed to transfer archive to Pi"

    Invoke-External -FilePath "ssh" -ArgumentList @("$PiUser@$PiHost", "tar -xf /tmp/.sync_bundle_$syncId.tar -C $TargetDir && rm -f /tmp/.sync_bundle_$syncId.tar") -ErrorMessage "Failed to extract archive on Pi"
    Invoke-External -FilePath "ssh" -ArgumentList @("$PiUser@$PiHost", "chmod -R u+rwX $TargetDir") -ErrorMessage "Failed to set write permissions on Pi target directory"

    if ($InstallRequirements) {
        Write-Host "Installing/updating Python dependencies in Pi virtual environment..."
        $venvInstallCmd = "cd $TargetDir && python3 -m venv $VenvDir && $VenvDir/bin/python -m pip install --upgrade pip && $VenvDir/bin/pip install -r requirements.txt"
        Invoke-External -FilePath "ssh" -ArgumentList @("$PiUser@$PiHost", $venvInstallCmd) -ErrorMessage "Failed to install requirements in Pi virtual environment"
    }

    if ($RunMain) {
        Write-Host "Starting main.py on Pi..."
        $runCmd = "cd $TargetDir && if [ -x $VenvDir/bin/python ]; then $VenvDir/bin/python main.py; else python3 main.py; fi"
        Invoke-External -FilePath "ssh" -ArgumentList @("-t", "$PiUser@$PiHost", $runCmd) -ErrorMessage "Failed to start main.py on Pi"
    }
}
finally {
    if (Test-Path -LiteralPath $archivePath) {
        Remove-Item -LiteralPath $archivePath -Force
    }
}

Write-Host "Sync complete."
