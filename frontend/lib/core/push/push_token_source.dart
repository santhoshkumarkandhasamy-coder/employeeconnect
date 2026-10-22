import 'package:flutter_riverpod/flutter_riverpod.dart';

/// Source of the device's FCM registration token.
///
/// There is no firebase_messaging dependency yet, so the default returns null
/// and logout skips the `DELETE /me/device-tokens/{fcm_token}` call. When
/// push lands, override [pushTokenSourceProvider] with an implementation that
/// returns `FirebaseMessaging.instance.getToken()`; logout will then remove
/// the token so a shared device stops receiving the previous user's pushes
/// (review B7/L7).
abstract class PushTokenSource {
  Future<String?> currentToken();
}

class NoPushTokenSource implements PushTokenSource {
  const NoPushTokenSource();

  @override
  Future<String?> currentToken() async => null;
}

final pushTokenSourceProvider =
    Provider<PushTokenSource>((ref) => const NoPushTokenSource());
