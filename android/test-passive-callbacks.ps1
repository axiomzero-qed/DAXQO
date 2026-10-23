$ErrorActionPreference = 'Stop'
$androidDir = $PSScriptRoot
$repoDir = Split-Path -Parent $androidDir
$jdk = Join-Path $repoDir '.tools/temurin-17/bin'
$sdkJar = Join-Path $repoDir '.tools/android-sdk/platforms/android-35/android.jar'
$classes = Join-Path $androidDir 'app/build/intermediates/javac/debug/compileDebugJavaWithJavac/classes'
$output = Join-Path $androidDir 'app/build/host-tests'
if (!(Test-Path -LiteralPath (Join-Path $classes 'io/daxqo/app/DeviceAccessibilityService.class'))) {
    throw 'Build the Android debug app before running the passive-callback regression.'
}
New-Item -ItemType Directory -Path $output -Force | Out-Null
& (Join-Path $jdk 'javac.exe') -encoding UTF-8 -cp "$classes;$sdkJar" -d $output (Join-Path $androidDir 'tests/PassiveCallbacksRegression.java')
if ($LASTEXITCODE -ne 0) { throw 'Host regression compilation failed.' }
& (Join-Path $jdk 'java.exe') -cp "$output;$classes;$sdkJar" io.daxqo.app.PassiveCallbacksRegression
if ($LASTEXITCODE -ne 0) { throw 'Passive callbacks unexpectedly accessed Android or changed control state.' }
