package __PACKAGE__

import android.os.Build
import android.view.WindowManager
import io.flutter.embedding.android.FlutterFragmentActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.File

// FlutterFragmentActivity is required by local_auth (biometric prompt).
class MainActivity : FlutterFragmentActivity() {

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        val messenger = flutterEngine.dartExecutor.binaryMessenger

        // Screenshot / screen recording / recents-thumbnail block (review H3, M10).
        // Dart side: lib/core/security/screen_protect.dart (reference counted).
        MethodChannel(messenger, "employeeconnect/screen").setMethodCallHandler { call, result ->
            when (call.method) {
                "secure" -> {
                    val on = call.arguments as? Boolean ?: true
                    runOnUiThread {
                        if (on) window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
                        else window.clearFlags(WindowManager.LayoutParams.FLAG_SECURE)
                        result.success(null)
                    }
                }
                else -> result.notImplemented()
            }
        }

        // Lightweight root heuristics (review H2). Bypassable by Magisk DenyList /
        // Frida: pair with Play Integrity verified server side for real assurance.
        MethodChannel(messenger, "employeeconnect/integrity").setMethodCallHandler { call, result ->
            when (call.method) {
                "isCompromised" -> result.success(isRooted())
                else -> result.notImplemented()
            }
        }
    }

    private fun isRooted(): Boolean {
        val tags = Build.TAGS
        if (tags != null && tags.contains("test-keys")) return true
        val paths = arrayOf(
            "/system/app/Superuser.apk", "/sbin/su", "/system/bin/su", "/system/xbin/su",
            "/data/local/xbin/su", "/data/local/bin/su", "/system/sd/xbin/su",
            "/system/bin/failsafe/su", "/data/local/su", "/su/bin/su", "/magisk/.core/bin/su",
            "/system/xbin/daemonsu", "/system/etc/init.d/99SuperSUDaemon",
        )
        if (paths.any { File(it).exists() }) return true
        return try {
            val p = Runtime.getRuntime().exec(arrayOf("/system/xbin/which", "su"))
            val found = p.inputStream.bufferedReader().readLine() != null
            p.destroy()
            found
        } catch (e: Exception) {
            false
        }
    }
}
