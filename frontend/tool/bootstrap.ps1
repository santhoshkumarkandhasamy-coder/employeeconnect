<#
.SYNOPSIS
  Generates the Flutter platform folders and applies the security hardening
  (review 01: H2, H3, H4, M10). Run from anywhere; operates on frontend/.

.DESCRIPTION
  1. flutter create (android + ios) in place, keeping lib/ and pubspec.yaml
  2. AndroidManifest: allowBackup=false, usesCleartextTraffic=false, network
     security config, data extraction rules, debug-only cleartext overlay (src/debug), INTERNET + USE_BIOMETRIC + location (fine/coarse)
  3. MainActivity.kt with FLAG_SECURE and root-check channels
  4. R8 minify/shrink + proguard rules, minSdk 23
  5. iOS AppDelegate (screen cover, capture detection, jailbreak check) and
     Info.plist (Face ID, location and camera texts; ATS exceptions removed)
  6. flutter pub get

  Idempotent: re-running does not duplicate attributes. Patches that cannot be
  matched (Flutter template changed) print a WARNING with what to do by hand,
  see PLATFORM_SETUP.md. NOTHING here has been executed against a real Flutter
  SDK by the author.

.PARAMETER Org      Reverse-DNS org (default com.employeeconnect)
.PARAMETER SkipIos  Skip the iOS patches (iOS builds need macOS anyway)
#>
[CmdletBinding()]
param(
  [string]$Org = 'com.employeeconnect',
  [switch]$SkipIos
)

$ErrorActionPreference = 'Stop'
$frontend = Split-Path -Parent $PSScriptRoot
$templates = Join-Path $PSScriptRoot 'templates'
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Write-Text($path, $text) { [IO.File]::WriteAllText($path, $text, $utf8) }
function Warn($msg) { Write-Warning $msg; $script:warnings++ }
$script:warnings = 0

if (-not (Get-Command flutter -ErrorAction SilentlyContinue)) {
  throw 'flutter is not on PATH. Install the Flutter SDK first (https://docs.flutter.dev/get-started/install).'
}

Push-Location $frontend
try {
  # 1. scaffolding ----------------------------------------------------------
  flutter create --org $Org --project-name employee_connect --platforms android,ios .
  if ($LASTEXITCODE -ne 0) { throw 'flutter create failed' }

  # 2. AndroidManifest ------------------------------------------------------
  $manifestPath = Join-Path $frontend 'android\app\src\main\AndroidManifest.xml'
  $m = [IO.File]::ReadAllText($manifestPath)

  function Set-AppAttr([string]$xml, [string]$name, [string]$value) {
    if ($xml -match "android:$name\s*=") {
      return [regex]::Replace($xml, "android:$name\s*=\s*`"[^`"]*`"", "android:$name=`"$value`"")
    }
    return [regex]::Replace($xml, '<application', "<application`n        android:$name=`"$value`"", 1)
  }
  $m = Set-AppAttr $m 'allowBackup' 'false'
  $m = Set-AppAttr $m 'usesCleartextTraffic' 'false'
  $m = Set-AppAttr $m 'networkSecurityConfig' '@xml/network_security_config'
  $m = Set-AppAttr $m 'dataExtractionRules' '@xml/data_extraction_rules'
  $m = Set-AppAttr $m 'fullBackupContent' 'false'
  # FINE + COARSE are both required: Android 12+ ignores a FINE-only request. Approximate-only grants are rejected in Dart.
  foreach ($perm in 'android.permission.INTERNET', 'android.permission.USE_BIOMETRIC', 'android.permission.ACCESS_FINE_LOCATION', 'android.permission.ACCESS_COARSE_LOCATION') {
    if ($m -notmatch [regex]::Escape($perm)) {
      $m = [regex]::Replace($m, '<application', "<uses-permission android:name=`"$perm`" />`n    <application", 1)
    }
  }
  Write-Text $manifestPath $m

  $xmlDir = Join-Path $frontend 'android\app\src\main\res\xml'
  New-Item -ItemType Directory -Force $xmlDir | Out-Null
  Copy-Item (Join-Path $templates 'network_security_config.xml') $xmlDir -Force
  Copy-Item (Join-Path $templates 'data_extraction_rules.xml') $xmlDir -Force

  # 2b. DEBUG-ONLY cleartext: debug manifest overlay + debug network config overlay.
  #     Release/profile keep the strict main config. Verify with the merged release manifest.
  $dbgDir = Join-Path $frontend 'android\app\src\debug'
  New-Item -ItemType Directory -Force (Join-Path $dbgDir 'res\xml') | Out-Null
  Copy-Item (Join-Path $templates 'AndroidManifest.debug.xml') (Join-Path $dbgDir 'AndroidManifest.xml') -Force
  Copy-Item (Join-Path $templates 'network_security_config.debug.xml') (Join-Path $dbgDir 'res\xml\network_security_config.xml') -Force

  # 3. MainActivity.kt ------------------------------------------------------
  $kt = Get-ChildItem (Join-Path $frontend 'android\app\src\main') -Recurse -Filter 'MainActivity.*' |
    Where-Object { $_.Extension -in '.kt', '.java' } | Select-Object -First 1
  if ($null -eq $kt) {
    Warn 'MainActivity not found; copy tool/templates/MainActivity.kt by hand.'
  } else {
    $old = [IO.File]::ReadAllText($kt.FullName)
    if ($old -match '(?m)^package\s+([\w.]+)') {
      $pkg = $Matches[1]
      $new = [IO.File]::ReadAllText((Join-Path $templates 'MainActivity.kt')).Replace('__PACKAGE__', $pkg)
      if ($kt.Extension -eq '.java') { Remove-Item $kt.FullName; $target = [IO.Path]::ChangeExtension($kt.FullName, '.kt') } else { $target = $kt.FullName }
      Write-Text $target $new
    } else {
      Warn 'Could not read the package of MainActivity; copy tool/templates/MainActivity.kt by hand.'
    }
  }

  # 4. Gradle: R8, minSdk 23, proguard -------------------------------------
  Copy-Item (Join-Path $templates 'proguard-rules.pro') (Join-Path $frontend 'android\app\proguard-rules.pro') -Force
  $gradle = Get-ChildItem (Join-Path $frontend 'android\app') -Filter 'build.gradle*' | Select-Object -First 1
  if ($null -eq $gradle) {
    Warn 'android/app/build.gradle(.kts) not found; enable R8 and minSdk 23 by hand.'
  } else {
    $g = [IO.File]::ReadAllText($gradle.FullName)
    $isKts = $gradle.Extension -eq '.kts'
    if ($g -notmatch 'proguard-rules\.pro') {
      if ($isKts) {
        $block = "release {`n            isMinifyEnabled = true`n            isShrinkResources = true`n            proguardFiles(getDefaultProguardFile(`"proguard-android-optimize.txt`"), `"proguard-rules.pro`")"
      } else {
        $block = "release {`n            minifyEnabled true`n            shrinkResources true`n            proguardFiles getDefaultProguardFile('proguard-android-optimize.txt'), 'proguard-rules.pro'"
      }
      $re = [regex]'release\s*\{'
      if ($re.IsMatch($g)) { $g = $re.Replace($g, $block, 1) } else { Warn 'No release buildType block found; enable R8 by hand.' }
    }
    $before = $g
    $g = [regex]::Replace($g, 'minSdk\s*=\s*flutter\.minSdkVersion', 'minSdk = 23')
    $g = [regex]::Replace($g, 'minSdkVersion\s+flutter\.minSdkVersion', 'minSdkVersion 23')
    if ($g -eq $before -and $g -notmatch 'minSdk(Version)?\s*=?\s*(2[3-9]|[3-9]\d)') { Warn 'minSdk not patched; set it to 23 or higher by hand.' }
    if ($g -match 'signingConfigs\.getByName\("debug"\)|signingConfig signingConfigs\.debug') {
      Warn 'Release build is still signed with the DEBUG key. Configure a real release keystore before shipping.'
    }
    Write-Text $gradle.FullName $g
  }

  # 4b. Root gradle: raise plugin modules to compileSdk 36 (file_picker 8.x is built against 34 but its
  #     androidx.lifecycle dependency requires 36; verified needed with Flutter 3.47 / AGP 9).
  $rootGradle = Join-Path $frontend 'android\build.gradle.kts'
  if (Test-Path $rootGradle) {
    $rg = [IO.File]::ReadAllText($rootGradle)
    if ($rg -notmatch 'LibraryExtension') {
      $hook = "subprojects {`n    afterEvaluate {`n        extensions.findByType(com.android.build.gradle.LibraryExtension::class.java)?.let {`n            if ((it.compileSdk ?: 0) < 36) it.compileSdk = 36`n        }`n    }`n}`n"
      $anchor = [regex]'(?m)^subprojects \{\r?\n\s*project\.evaluationDependsOn'
      if ($anchor.IsMatch($rg)) { Write-Text $rootGradle ($anchor.Replace($rg, { param($m) $hook + $m.Value }, 1)) }
      else { Warn 'android/build.gradle.kts: add the compileSdk 36 hook for plugin modules by hand (see git history / report).' }
    }
  } else { Warn 'android/build.gradle.kts not found (Groovy template?); raise plugin compileSdk to 36 by hand.' }

  # 5. iOS ------------------------------------------------------------------
  if (-not $SkipIos) {
    $delegate = Join-Path $frontend 'ios\Runner\AppDelegate.swift'
    if (Test-Path $delegate) { Copy-Item (Join-Path $templates 'AppDelegate.swift') $delegate -Force }
    else { Warn 'ios/Runner/AppDelegate.swift not found; merge tool/templates/AppDelegate.swift by hand.' }

    $plistPath = Join-Path $frontend 'ios\Runner\Info.plist'
    if (Test-Path $plistPath) {
      $p = [IO.File]::ReadAllText($plistPath)
      # ATS must stay at its secure default: remove any exception block.
      $p = [regex]::Replace($p, '(?s)\s*<key>NSAppTransportSecurity</key>\s*<dict>.*?</dict>', '')
      if ($p -notmatch 'NSFaceIDUsageDescription') {
        $p = $p -replace '</dict>\s*</plist>', "`t<key>NSFaceIDUsageDescription</key>`n`t<string>Unlock Employee Connect with Face ID.</string>`n</dict>`n</plist>"
      }
      $usage = @{
        NSLocationWhenInUseUsageDescription = 'Employee Connect uses your location when you check in or out to confirm you are at the office.'
        NSCameraUsageDescription            = 'Employee Connect uses the camera to take an optional selfie when you check in.'
      }
      foreach ($k in $usage.Keys) {
        if ($p -notmatch $k) {
          $p = $p -replace '</dict>\s*</plist>', "`t<key>$k</key>`n`t<string>$($usage[$k])</string>`n</dict>`n</plist>"
        }
      }
      Write-Text $plistPath $p
    } else {
      Warn 'ios/Runner/Info.plist not found.'
    }
  }

  # 6. dependencies ---------------------------------------------------------
  flutter pub get
  if ($LASTEXITCODE -ne 0) { throw 'flutter pub get failed' }
}
finally {
  Pop-Location
}

Write-Host ''
Write-Host "Done with $($script:warnings) warning(s). Next:"
Write-Host '  flutter analyze; flutter test'
Write-Host '  Release builds MUST pass --dart-define=API_BASE_URL=https://... and --dart-define=CERT_PINS=<pin1>,<pin2>'
Write-Host '  Verify on a device: screenshots blocked on login, recents thumbnail blank, adb backup empty.'
