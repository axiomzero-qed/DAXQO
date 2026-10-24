param([ValidateSet('standard','xm')][string]$Platform = 'standard')
$ErrorActionPreference = 'Stop'
$repoDir = Split-Path -Parent $PSScriptRoot
$usedDrives = (Get-PSDrive -PSProvider FileSystem).Name
$driveLetter = @('R','S','T','U','V','W','X','Y','Z') | Where-Object { $_ -notin $usedDrives } | Select-Object -First 1
if (!$driveLetter) { throw 'No free temporary drive letter is available for APK inspection.' }
$drive = "$($driveLetter):"
& subst.exe $drive $repoDir
if ($LASTEXITCODE -ne 0) { throw 'Could not create an ASCII path for Android SDK tools.' }
try {
$mappedRoot = $drive + '\'
$apk = Join-Path $mappedRoot 'android/app/build/outputs/apk/debug/app-debug.apk'
$sdkDir = Join-Path $mappedRoot '.tools/android-sdk'
$buildTools = Get-ChildItem (Join-Path $sdkDir 'build-tools') -Directory | Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1
$aapt = Join-Path $buildTools.FullName 'aapt.exe'
$analyzer = Join-Path $sdkDir 'cmdline-tools/latest/bin/apkanalyzer.bat'
$javaDir = Join-Path $repoDir '.tools/temurin-17'
if (!(Test-Path $apk)) { throw "APK not found: $apk" }
if (!(Test-Path $aapt) -or !(Test-Path $analyzer) -or !(Test-Path (Join-Path $javaDir 'bin/java.exe'))) { throw 'Bundled Android SDK/JDK tools are incomplete.' }
$env:JAVA_HOME = $javaDir
$env:PATH = (Join-Path $javaDir 'bin') + ';' + $env:PATH
$expected = if ($Platform -eq 'xm') {
    @{ package = 'io.daxqo.xm'; version = '0.6.1-xm'; scheme = 'daxqo-xm'; target = 'io.daxqo.xm' }
} else {
    @{ package = 'io.daxqo.app'; version = '0.6.1'; scheme = 'daxqo'; target = 'io.daxqo.app' }
}
$badging = (& $aapt dump badging $apk 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) { throw "aapt dump badging failed: $badging" }
if ($badging -notmatch "(?m)^package: name='$([regex]::Escape($expected.package))' versionCode='11' versionName='$([regex]::Escape($expected.version))'") {
    throw "APK package/version does not match $Platform profile."
}
$manifest = (& $aapt dump xmltree $apk AndroidManifest.xml 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) { throw "aapt dump xmltree failed: $manifest" }
if ($manifest -notmatch "android:scheme\(.*?\)=`"$([regex]::Escape($expected.scheme))`"" -or $manifest -notmatch 'android:host\(.*?\)="connect"') {
    throw "APK pairing scheme does not match $Platform profile."
}
foreach ($resource in @(@{ name = 'platform_id'; value = $Platform }, @{ name = 'pairing_scheme'; value = $expected.scheme }, @{ name = 'shortcut_target_package'; value = $expected.target })) {
    $value = (& $analyzer resources value $apk --config default --type string --name $resource.name --package $expected.package 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Could not read APK string resource $($resource.name): $value" }
    if ($value -ne $resource.value) { throw "APK $($resource.name) is '$value'; expected '$($resource.value)'." }
}
Write-Host "APK profile verified: $Platform ($($expected.package), $($expected.version), $($expected.scheme))."
} finally {
    & subst.exe $drive /D | Out-Null
}
