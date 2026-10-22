import 'dart:async';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

/// Widget tests run on fake async: a method call on a channel with no mock
/// handler never completes there, so every `SecureScreen` would sit on its
/// spinner forever. Install a no-op handler for the native screen-protect
/// channel before each test; tests that care (platform_security_test) replace it.
Future<void> testExecutable(FutureOr<void> Function() testMain) async {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(
      const MethodChannel('employeeconnect/screen'),
      (_) async => null,
    );
  });
  await testMain();
}
