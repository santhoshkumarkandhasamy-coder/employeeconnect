# R8 / ProGuard rules for Employee Connect (release). Flutter's own rules are
# added automatically by the Flutter Gradle plugin; these cover plugins we use.

# flutter_secure_storage (EncryptedSharedPreferences / Tink)
-keep class com.it_nomads.fluttersecurestorage.** { *; }
-keep class androidx.security.crypto.** { *; }
-keep class com.google.crypto.tink.** { *; }
-dontwarn com.google.errorprone.annotations.**
-dontwarn javax.annotation.**

# local_auth (androidx.biometric)
-keep class androidx.biometric.** { *; }
-keep class io.flutter.plugins.localauth.** { *; }

# Our MethodChannel host activity
-keep class **.MainActivity { *; }

# Strip logging in release
-assumenosideeffects class android.util.Log {
    public static int v(...);
    public static int d(...);
    public static int i(...);
}
