abstract final class AppConstants {
  static const String appName = 'Employee Connect';

  /// Defaults used until `/config` has been fetched (auth.inactivity_timeout_minutes).
  static const int defaultInactivityMinutes = 5;

  /// Full logout when the app has been in the background longer than this.
  static const Duration backgroundLogout = Duration(minutes: 30);

  static const Duration connectTimeout = Duration(seconds: 15);
  static const Duration receiveTimeout = Duration(seconds: 20);
}
