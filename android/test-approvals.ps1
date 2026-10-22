$ErrorActionPreference = 'Stop'
$androidDir = $PSScriptRoot
$repoDir = Split-Path -Parent $androidDir
$jdk = Join-Path $repoDir '.tools/temurin-17/bin'
$sdkJar = Join-Path $repoDir '.tools/android-sdk/platforms/android-35/android.jar'
$classes = Join-Path $androidDir 'app/build/intermediates/javac/debug/compileDebugJavaWithJavac/classes'
$output = Join-Path $androidDir 'app/build/host-tests'
if (!(Test-Path -LiteralPath (Join-Path $classes 'io/daxqo/app/ApprovalLedger.class'))) {
    throw 'Build the Android debug app before running the approval regression.'
}
New-Item -ItemType Directory -Path $output -Force | Out-Null
& (Join-Path $jdk 'javac.exe') -encoding UTF-8 -cp "$classes;$sdkJar" -d $output (Join-Path $androidDir 'tests/ApprovalLedgerRegression.java')
if ($LASTEXITCODE -ne 0) { throw 'Approval host regression compilation failed.' }
& (Join-Path $jdk 'java.exe') -cp "$output;$classes;$sdkJar" io.daxqo.app.ApprovalLedgerRegression
if ($LASTEXITCODE -ne 0) { throw 'Approval regression failed.' }
# Inspect the built APK, not only the source manifest: wake must not join the
# main DAXQO task or acquire an externally callable entry point.
Push-Location $repoDir
try {
    $manifestDump = (& '.\.tools\android-sdk\build-tools\35.0.0\aapt2.exe' dump xmltree '.\android\app\build\outputs\apk\debug\app-debug.apk' --file AndroidManifest.xml) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the packaged Android manifest.' }
    $blocks = [regex]::Matches($manifestDump, '(?ms)^\s*E: activity\b.*?(?=^\s*E: (?:activity|service|provider|receiver)\b|\z)')
    $wake = @($blocks | Where-Object { $_.Value -match 'android:name[^\n]*="io\.daxqo\.app\.WakeActivity"' })
    if ($wake.Count -ne 1 -or $wake[0].Value -notmatch 'android:taskAffinity[^\n]*=""' -or $wake[0].Value -notmatch 'android:exported[^\n]*=false' -or $wake[0].Value -notmatch 'android:excludeFromRecents[^\n]*=true') {
        throw 'Packaged WakeActivity must be private, isolated from the main task and excluded from Recents.'
    }
    Write-Host 'PASS: packaged WakeActivity has empty taskAffinity, private entry and no Recents entry.'
} finally { Pop-Location }
