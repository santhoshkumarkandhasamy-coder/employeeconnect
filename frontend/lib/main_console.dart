import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'console/console_api.dart';
import 'console/console_app.dart';
import 'console/console_auth.dart';
import 'core/storage/app_state_store.dart';

/// Admin web console entrypoint (Flutter web, served at /console/):
///
///   flutter build web --release -t lib/main_console.dart --base-href /console/ \
///     --no-web-resources-cdn --pwa-strategy none
///
/// Reuses the admin data layer and screens. Sign-in goes through
/// `/api/auth/web/*` (HttpOnly refresh cookie, access token in memory only,
/// header `X-EC-Client: console`). No certificate pinning in a browser (the
/// browser trust store applies) and no device storage: branding is kept in
/// memory only.
void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(
    ProviderScope(
      overrides: [
        ...consoleAdminOverrides(
          (ref) => createConsoleDio(
            tokens: ref.read(consoleTokensProvider),
            onSessionEnded: (code) => ref.read(consoleAuthProvider.notifier).sessionEnded(code),
            onPasswordChangeRequired: () =>
                ref.read(consoleAuthProvider.notifier).passwordChangeRequired(),
          ),
        ),
        appStateStoreProvider.overrideWithValue(MemoryAppStateStore()),
      ],
      child: const ConsoleApp(),
    ),
  );
}
