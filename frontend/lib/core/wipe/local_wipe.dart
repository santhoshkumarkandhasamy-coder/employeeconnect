import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/painting.dart';
import 'package:path_provider/path_provider.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../features/attendance/data/selfie_service.dart';
import '../../features/food/data/food_repository.dart' show exportStoreProvider;
import '../cache/json_cache.dart';
import '../files/file_service.dart';
import '../server/branding.dart';
import '../storage/app_state_store.dart';
import '../storage/secure_storage.dart';

/// Removes everything this app stored for the signed-in user: tokens and all
/// other secure-storage keys, the JSON cache, export / share copies, picked
/// selfies, in-memory and image caches. Idempotent; every step is best effort
/// so one failing step never leaves the rest behind.
///
/// [keepServer] keeps the server URL and cached branding (offboarding,
/// session loss); "Change server" passes false.
class LocalWipe {
  LocalWipe(this._ref);
  final Ref _ref;

  static Future<void> _step(Future<void> Function() f) async {
    try {
      await f();
    } on Object {
      // best effort
    }
  }

  Future<void> all({required bool keepServer}) async {
    await _step(() => _ref.read(tokenStoreProvider).clearSession());
    await _step(() => _ref.read(appStateStoreProvider).wipe(keepServer: keepServer));
    await cachesOnly();
    if (!keepServer) await _step(() => _ref.read(brandingProvider.notifier).clear());
  }

  /// Cached data only (tokens stay): used when the offline limit is exceeded.
  Future<void> cachesOnly() async {
    const step = _step;
    await step(() => _ref.read(jsonCacheProvider).clear());
    await step(() => _ref.read(exportStoreProvider).clear());
    await step(() => _ref.read(selfieServiceProvider).purgeAll());
    await step(() async => _ref.read(fileServiceProvider).clearMemory());
    await step(() async {
      PaintingBinding.instance.imageCache
        ..clear()
        ..clearLiveImages();
    });
    // image_picker / file_picker leave copies of picked files in the temp and
    // app cache dirs (leave attachments, announcement PDFs, logos).
    await step(() async {
      for (final dir in await _ref.read(wipeCacheDirsProvider)()) {
        await step(() => _clearChildren(dir));
      }
    });
    await step(() async {
      if (Platform.isAndroid || Platform.isIOS) {
        await FilePicker.platform.clearTemporaryFiles();
      }
    });
  }

  static Future<void> _clearChildren(Directory dir) async {
    if (!dir.existsSync()) return;
    await for (final e in dir.list()) {
      await _step(() async {
        await e.delete(recursive: true);
      });
    }
  }
}

/// Directories whose contents are deleted by the wipe (overridable in tests).
final wipeCacheDirsProvider = Provider<Future<List<Directory>> Function()>((ref) {
  return () async => [
        await getTemporaryDirectory(),
        await getApplicationCacheDirectory(),
      ];
});

final localWipeProvider = Provider<LocalWipe>(LocalWipe.new);
