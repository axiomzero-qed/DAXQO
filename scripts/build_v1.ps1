param(
    [Parameter(Mandatory=$true)][string]$MotorSource,
    [Parameter(Mandatory=$true)][string]$CollectorSource,
    [string]$InputDirectory,
    [switch]$RebuildEnvironment,
    [switch]$Lint,
    [switch]$Release
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$version = (Get-Content -LiteralPath (Join-Path $repo 'VERSION') -Raw).Trim()
if (!$InputDirectory) { $InputDirectory = Join-Path $repo 'dist\deploy-cn\packages' }
$offline = Join-Path $repo '.tools\offline-runtime'
$mail = Join-Path $repo '.tools\mail-libs'
$releaseDirectory = Join-Path $repo "dist\v$version"
$mappedDrive = $null
$alignedApk = $null
$signedApk = $null
$keepSignedApk = $false
$oldJavaHome = $env:JAVA_HOME
$oldAndroidHome = $env:ANDROID_HOME
$oldPath = $env:PATH
Push-Location $repo
try {
    & python scripts\fetch_offline_inputs.py --output-dir $InputDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Pinned offline build-input preparation failed.' }

    New-Item -ItemType Directory -Force -Path $offline | Out-Null
    & python scripts\prepare_embedded_native.py --cache-dir .tools\offline-inputs --output-dir $offline
    if ($LASTEXITCODE -ne 0) { throw 'Native runtime preparation failed.' }

    if (!(Test-Path -LiteralPath (Join-Path $mail 'dkim\__init__.py')) -or
        !(Test-Path -LiteralPath (Join-Path $mail 'dns\__init__.py'))) {
        New-Item -ItemType Directory -Force -Path $mail | Out-Null
        & python -m pip install --disable-pip-version-check --target $mail --no-compile --require-hashes --no-deps -r scripts\mail-requirements.txt
        if ($LASTEXITCODE -ne 0) { throw 'Pinned mail library preparation failed.' }
    }

    $manifest = Join-Path $offline 'assets\runtime\manifest.json'
    $environmentCurrent = $false
    if (!$RebuildEnvironment -and (Test-Path -LiteralPath $manifest)) {
        & python scripts\build_offline_runtime.py --rootfs (Join-Path $InputDirectory 'noble-server-cloudimg-arm64-root.tar.xz') `
            --codex (Join-Path $InputDirectory 'codex-0.163.0-linux-arm64.tgz') `
            --motor-source (Join-Path $MotorSource 'motor') --collector-source (Join-Path $CollectorSource 'trajectory') `
            --mail-libs $mail --output-dir $offline --check-current
        $environmentCurrent = $LASTEXITCODE -eq 0
    }
    if (!$environmentCurrent) {
        & python scripts\build_offline_runtime.py --rootfs (Join-Path $InputDirectory 'noble-server-cloudimg-arm64-root.tar.xz') `
            --codex (Join-Path $InputDirectory 'codex-0.163.0-linux-arm64.tgz') `
            --motor-source (Join-Path $MotorSource 'motor') --collector-source (Join-Path $CollectorSource 'trajectory') `
            --mail-libs $mail --output-dir $offline
        if ($LASTEXITCODE -ne 0) { throw 'Offline environment build failed.' }
    }

    & python scripts\prepare_runtime_licenses.py --source-dir .tools\offline-inputs\runtime-sources `
        --output-dir $offline --release-dir $releaseDirectory --rootfs-zip (Join-Path $offline 'assets\runtime\rootfs.zip')
    if ($LASTEXITCODE -ne 0) { throw 'Runtime license preparation failed.' }

    & (Join-Path $repo 'android\build.ps1') -Lint:$Lint -Release:$Release
    if ($LASTEXITCODE -ne 0) { throw 'APK build failed.' }

    # Android's native APK tools cannot reliably open the repository's Unicode path.
    $usedDrives = (Get-PSDrive -PSProvider FileSystem).Name
    $driveLetter = @('R','S','T','U','V','W','X','Y','Z') | Where-Object { $_ -notin $usedDrives } | Select-Object -First 1
    if (!$driveLetter) { throw 'No free temporary drive letter is available for APK alignment.' }
    $mappedDrive = "$($driveLetter):"
    & subst.exe $mappedDrive $repo
    if ($LASTEXITCODE -ne 0) { throw 'Could not create a temporary ASCII path for APK signing tools.' }
    $mappedRoot = $mappedDrive + '\'
    $buildTools = Join-Path $mappedRoot '.tools\android-sdk\build-tools\35.0.0'
    $zipalign = Join-Path $buildTools 'zipalign.exe'
    $apksigner = Join-Path $buildTools 'apksigner.bat'
    $sourceApk = Join-Path $mappedRoot $(if ($Release) { 'android\app\build\outputs\apk\release\app-release-unsigned.apk' } else { 'android\app\build\outputs\apk\debug\app-debug.apk' })
    $mappedRelease = Join-Path $mappedRoot "dist\v$version"
    $finalMappedApk = Join-Path $mappedRelease "DAXQO-$version-arm64.apk"
    $debugKey = Join-Path $env:USERPROFILE '.android\debug.keystore'
    if (!(Test-Path -LiteralPath $debugKey)) { throw 'The existing local Android debug signing key is unavailable.' }
    New-Item -ItemType Directory -Force -Path $mappedRelease | Out-Null
    $stageId = [Guid]::NewGuid().ToString('N')
    $alignedApk = Join-Path $mappedRelease ".DAXQO-$stageId-aligned.apk"
    $signedApk = Join-Path $mappedRelease ".DAXQO-$stageId-signed.apk"
    $env:JAVA_HOME = Join-Path $mappedRoot '.tools\temurin-17'
    $env:ANDROID_HOME = Join-Path $mappedRoot '.tools\android-sdk'
    $env:PATH = (Join-Path $env:JAVA_HOME 'bin') + ';' + $oldPath

    & $zipalign -f -p 4 $sourceApk $alignedApk
    if ($LASTEXITCODE -ne 0) { throw 'APK compaction and alignment failed.' }
    & $apksigner sign --v4-signing-enabled false --ks $debugKey --ks-key-alias androiddebugkey --ks-pass pass:android --key-pass pass:android --out $signedApk $alignedApk
    if ($LASTEXITCODE -ne 0) { throw 'APK signing with the existing local debug key failed.' }
    & $apksigner verify --verbose --print-certs $signedApk
    if ($LASTEXITCODE -ne 0) { throw 'APK signature verification failed.' }
    & $zipalign -c 4 $signedApk
    if ($LASTEXITCODE -ne 0) { throw 'Final APK alignment verification failed.' }
    & python (Join-Path $repo 'scripts\check_offline_apk.py') --apk $signedApk
    if ($LASTEXITCODE -ne 0) { throw 'Offline APK verification failed; the previous APK was preserved.' }
    Move-Item -LiteralPath $signedApk -Destination $finalMappedApk -Force
    $signedApk = $finalMappedApk
    Remove-Item -LiteralPath $alignedApk -Force
    $keepSignedApk = $true
} finally {
    if ($alignedApk -and (Test-Path -LiteralPath $alignedApk)) { Remove-Item -LiteralPath $alignedApk -Force }
    if (!$keepSignedApk -and $signedApk -and (Test-Path -LiteralPath $signedApk)) { Remove-Item -LiteralPath $signedApk -Force }
    if ($mappedDrive) { & subst.exe $mappedDrive /D | Out-Null }
    $env:JAVA_HOME = $oldJavaHome
    $env:ANDROID_HOME = $oldAndroidHome
    $env:PATH = $oldPath
    Pop-Location
}

$finalApk = Join-Path $releaseDirectory "DAXQO-$version-arm64.apk"
& python (Join-Path $repo 'scripts\check_offline_apk.py') --apk $finalApk
if ($LASTEXITCODE -ne 0) { throw 'Offline APK verification failed.' }
Get-FileHash -Algorithm SHA256 -LiteralPath $finalApk
