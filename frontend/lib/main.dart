import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path_provider/path_provider.dart';

import 'app/app.dart';
import 'core/server/server_providers.dart';
import 'core/server/server_url.dart';
import 'core/storage/app_state_store.dart';
import 'core/files/share_file_opener.dart';
import 'features/food/data/food_repository.dart'
    show exportDirProvider, exportStoreProvider, fileOpenerProvider;

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  // App-private cache dir from path_provider (not Directory.systemTemp).
  // share_plus copies shared files to `<this dir>/share_plus`, which the export
  // store wipes as well. Review 08 L6 / M1.
  final cache = await getTemporaryDirectory();
  // Saved server URL (survives wipes) > build-time API_BASE_URL > Server setup.
  String? saved;
  try {
    saved = await SecureAppStateStore().readServerUrl();
  } on Object {
    saved = null;
  }
  final baseUrl = resolveBaseUrl(saved: saved);
  runApp(ProviderScope(
    overrides: [
      baseUrlProvider.overrideWith((ref) => baseUrl),
      serverSetupRequiredProvider.overrideWith((ref) => baseUrl == null),
      exportDirProvider.overrideWithValue(
        Directory('${cache.path}${Platform.pathSeparator}ec_exports'),
      ),
      fileOpenerProvider.overrideWith(
        (ref) => ShareFileOpener(store: ref.watch(exportStoreProvider)),
      ),
    ],
    child: const EmployeeConnectApp(),
  ),);
}
