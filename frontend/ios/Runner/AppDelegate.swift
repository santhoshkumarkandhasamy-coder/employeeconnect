import Flutter
import UIKit

// Classic Flutter AppDelegate template. If your Flutter version generated a
// SceneDelegate / FlutterImplicitEngineDelegate template, merge the channel
// setup and the lifecycle overrides below into it instead of replacing.
@main
@objc class AppDelegate: FlutterAppDelegate {
  private var secureEnabled = false
  private var coverView: UIView?

  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    GeneratedPluginRegistrant.register(with: self)

    if let controller = window?.rootViewController as? FlutterViewController {
      let messenger = controller.binaryMessenger

      // Screen protection (review H3): while "secure", cover the window during
      // screen recording / mirroring (UIScreen.isCaptured).
      FlutterMethodChannel(name: "employeeconnect/screen", binaryMessenger: messenger)
        .setMethodCallHandler { [weak self] call, result in
          if call.method == "secure" {
            self?.secureEnabled = (call.arguments as? Bool) ?? true
            self?.updateCapturedCover()
            result(nil)
          } else {
            result(FlutterMethodNotImplemented)
          }
        }

      // Lightweight jailbreak heuristics (review H2).
      FlutterMethodChannel(name: "employeeconnect/integrity", binaryMessenger: messenger)
        .setMethodCallHandler { call, result in
          if call.method == "isCompromised" {
            result(AppDelegate.isJailbroken())
          } else {
            result(FlutterMethodNotImplemented)
          }
        }
    }

    NotificationCenter.default.addObserver(
      forName: UIScreen.capturedDidChangeNotification, object: nil, queue: .main
    ) { [weak self] _ in self?.updateCapturedCover() }

    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }

  // App switcher snapshot: blank the UI as soon as the app resigns active.
  override func applicationWillResignActive(_ application: UIApplication) {
    showCover()
    super.applicationWillResignActive(application)
  }

  override func applicationDidBecomeActive(_ application: UIApplication) {
    if !(secureEnabled && UIScreen.main.isCaptured) { hideCover() }
    super.applicationDidBecomeActive(application)
  }

  private func updateCapturedCover() {
    if secureEnabled && UIScreen.main.isCaptured { showCover() } else { hideCover() }
  }

  private func showCover() {
    guard coverView == nil, let window = self.window else { return }
    let v = UIView(frame: window.bounds)
    v.backgroundColor = .systemBackground
    v.autoresizingMask = [.flexibleWidth, .flexibleHeight]
    window.addSubview(v)
    coverView = v
  }

  private func hideCover() {
    coverView?.removeFromSuperview()
    coverView = nil
  }

  private static func isJailbroken() -> Bool {
    #if targetEnvironment(simulator)
    return false
    #else
    let paths = [
      "/Applications/Cydia.app", "/Library/MobileSubstrate/MobileSubstrate.dylib",
      "/bin/bash", "/usr/sbin/sshd", "/etc/apt", "/private/var/lib/apt/",
      "/usr/bin/ssh", "/var/jb",
    ]
    if paths.contains(where: { FileManager.default.fileExists(atPath: $0) }) { return true }
    let probe = "/private/employeeconnect_probe.txt"
    do {
      try "x".write(toFile: probe, atomically: true, encoding: .utf8)
      try? FileManager.default.removeItem(atPath: probe)
      return true // wrote outside the sandbox
    } catch {
      return false
    }
    #endif
  }
}
