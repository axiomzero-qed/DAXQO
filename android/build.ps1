param([switch]$InstallTools, [switch]$Lint, [switch]$Xm, [ValidateSet('standard','xm')][string]$Platform)
$ErrorActionPreference = 'Stop'
if ($PSBoundParameters.ContainsKey('Platform') -and $Xm -and $Platform -ne 'xm') {
    throw 'Conflicting -Xm and -Platform values.'
}
$androidDir = $PSScriptRoot
$repoDir = Split-Path -Parent $androidDir
$toolsDir = Join-Path $repoDir '.tools'
$jdkDir = Join-Path $toolsDir 'temurin-17'
$gradleDir = Join-Path $toolsDir 'gradle-8.10.2'
$sdkDir = Join-Path $toolsDir 'android-sdk'

function Get-File($Uri, $Path) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    Invoke-WebRequest -Uri $Uri -OutFile $Path
}
function Expand-Zip($Archive, $Destination) {
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Expand-Archive -LiteralPath $Archive -DestinationPath $Destination -Force
}

if ($InstallTools -or !(Test-Path (Join-Path $jdkDir 'bin\javac.exe')) -or !(Test-Path (Join-Path $gradleDir 'bin\gradle.bat')) -or !(Test-Path (Join-Path $sdkDir 'cmdline-tools\latest\bin\sdkmanager.bat'))) {
    New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null

    if (!(Test-Path (Join-Path $jdkDir 'bin\javac.exe'))) {
        $api = 'https://api.adoptium.net/v3/assets/latest/17/hotspot?architecture=x64&image_type=jdk&os=windows&vendor=eclipse'
        $release = (Invoke-RestMethod -Uri $api)[0]
        $pkg = $release.binary.package
        $archive = Join-Path $toolsDir 'temurin-jdk17.zip'
        Get-File $pkg.link $archive
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
        if ($actual -ne $pkg.checksum.ToLowerInvariant()) { throw 'Temurin JDK SHA-256 check failed.' }
        $unpack = Join-Path $toolsDir 'temurin-unpack'
        Expand-Zip $archive $unpack
        $jdkRoot = Get-ChildItem -Directory $unpack | Select-Object -First 1
        Move-Item -LiteralPath $jdkRoot.FullName -Destination $jdkDir -Force
        Remove-Item -LiteralPath $unpack -Recurse -Force
    }

    if (!(Test-Path (Join-Path $gradleDir 'bin\gradle.bat'))) {
        $archive = Join-Path $toolsDir 'gradle-8.10.2-bin.zip'
        Get-File 'https://services.gradle.org/distributions/gradle-8.10.2-bin.zip' $archive
        $sumFile = "$archive.sha256"
        Get-File 'https://services.gradle.org/distributions/gradle-8.10.2-bin.zip.sha256' $sumFile
        $expected = ((Get-Content -Raw $sumFile).Trim() -split '\s+')[0].ToLowerInvariant()
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
        if ($actual -ne $expected) { throw 'Gradle SHA-256 check failed.' }
        Expand-Zip $archive $toolsDir
    }

    if (!(Test-Path (Join-Path $sdkDir 'cmdline-tools\latest\bin\sdkmanager.bat'))) {
        $archive = Join-Path $toolsDir 'android-commandlinetools.zip'
        Get-File 'https://dl.google.com/android/repository/commandlinetools-win-15859902_latest.zip' $archive
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
        if ($actual -ne '90ae805d20434428bffcb699c290860f19bb5f66a67e6b330067e3de801fb04a') { throw 'Android command line tools SHA-256 check failed.' }
        $staging = Join-Path $toolsDir 'android-cli-unpack'
        Expand-Zip $archive $staging
        New-Item -ItemType Directory -Force -Path (Join-Path $sdkDir 'cmdline-tools') | Out-Null
        Move-Item -LiteralPath (Join-Path $staging 'cmdline-tools') -Destination (Join-Path $sdkDir 'cmdline-tools\latest') -Force
        Remove-Item -LiteralPath $staging -Recurse -Force
    }

    $env:JAVA_HOME = $jdkDir
    $env:ANDROID_HOME = $sdkDir
    $sdkmanager = Join-Path $sdkDir 'cmdline-tools\latest\bin\sdkmanager.bat'
    1..50 | ForEach-Object { 'y' } | & $sdkmanager --sdk_root=$sdkDir --licenses | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Android SDK license acceptance failed.' }
    & $sdkmanager --sdk_root=$sdkDir 'platform-tools' 'platforms;android-35' 'build-tools;35.0.0'
    if ($LASTEXITCODE -ne 0) { throw 'Android SDK components could not be installed.' }
}

$env:JAVA_HOME = $jdkDir
$env:ANDROID_HOME = $sdkDir
$usedDrives = (Get-PSDrive -PSProvider FileSystem).Name
$driveLetter = @('R','S','T','U','V','W','X','Y','Z') | Where-Object { $_ -notin $usedDrives } | Select-Object -First 1
if (!$driveLetter) { throw 'No free temporary drive letter is available for the build.' }
$drive = "$($driveLetter):"
& subst.exe $drive $repoDir
if ($LASTEXITCODE -ne 0) { throw 'Could not create a temporary ASCII path for Android tools.' }
$mappedRoot = $drive + '\'
$mappedTools = Join-Path $mappedRoot '.tools'
$mappedAndroidDir = Join-Path $mappedRoot 'android'
$env:JAVA_HOME = Join-Path $mappedTools 'temurin-17'
$env:ANDROID_HOME = Join-Path $mappedTools 'android-sdk'
$env:PATH = (Join-Path $env:JAVA_HOME 'bin') + ';' + $env:PATH
$localProps = Join-Path $mappedAndroidDir 'local.properties'
"sdk.dir=$($env:ANDROID_HOME.Replace('\','\\'))" | Set-Content -Encoding ASCII $localProps
Push-Location $mappedAndroidDir
try {
    $buildTasks = @()
    if ($PSBoundParameters.ContainsKey('Platform')) {
        $buildTasks += "-Pplatform=$Platform"
    } elseif ($Xm) { $buildTasks += '-Pxm=true' }
    $buildTasks += 'assembleDebug'
    if ($Lint) { $buildTasks += 'lintDebug' }
    & (Join-Path $mappedTools 'gradle-8.10.2\bin\gradle.bat') --no-daemon @buildTasks
    if ($LASTEXITCODE -ne 0) { throw 'Android debug APK build failed.' }
} finally {
    Pop-Location
    & subst.exe $drive /D | Out-Null
}
Write-Host "APK: $androidDir\app\build\outputs\apk\debug\app-debug.apk"
