import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/dio_client.dart';
import '../../../core/cache/json_cache.dart';
import '../../attendance/data/attendance_repository.dart' show cacheScopeProvider;
import '../../auth/presentation/auth_controller.dart';
import '../../auth/presentation/auth_state.dart';
import '../domain/food_models.dart';

/// api-contract.md section 6 (food) and section 10 (export).
/// The list of my enrollments is cached offline (same JSON cache as the other
/// features: cleared on logout / lock, scoped per user).
class FoodRepository {
  FoodRepository(this._dio, this._cache, {this.scope = 'anon', DateTime Function()? now})
      : _now = now ?? DateTime.now;

  final Dio _dio;
  final JsonCache _cache;
  final String scope;
  final DateTime Function() _now;

  /// Server clock minus device clock, learned from the `Date` response header
  /// of the latest successful call. Zero until a call succeeds.
  Duration serverOffset = Duration.zero;

  String get _key => '$scope.food.mine';

  void _learnOffset(Response<dynamic> res) {
    final h = res.headers.value('date');
    if (h == null) return;
    try {
      serverOffset = HttpDate.parse(h).difference(_now());
    } on Object {
      // keep the previous offset
    }
  }

  Future<Cached<List<FoodDay>>> myEnrollments() => fetchWithCache(
        cache: _cache,
        key: _key,
        fetch: () => guardApi<Object>(() async {
          final res = await _dio.get<Map<String, dynamic>>('/food/my-enrollments');
          _learnOffset(res);
          return res.data ?? const <String, dynamic>{};
        }),
        parse: FoodDay.parseList,
      );

  /// Persists the latest list after a successful change (best effort).
  Future<void> remember(List<FoodDay> days) =>
      _cache.write(_key, {'items': [for (final d in days) d.toJson()]});

  Future<FoodDay> setOne(String date, {required bool needFood}) =>
      guardApi<FoodDay>(() async {
        final res = await _dio.put<Map<String, dynamic>>(
          '/food/enrollment',
          data: {'date': date, 'need_food': needFood},
        );
        _learnOffset(res);
        return FoodDay.tryParse(res.data) ?? FoodDay(date: date, needFood: needFood);
      });

  /// Bulk set (max 31 per call). Partial success: see [FoodBulkResult.failed].
  Future<FoodBulkResult> setMany(List<String> dates, {required bool needFood}) =>
      guardApi<FoodBulkResult>(() async {
        final res = await _dio.put<Map<String, dynamic>>(
          '/food/enrollments',
          data: {
            'items': [for (final d in dates.take(31)) {'date': d, 'need_food': needFood}],
          },
        );
        _learnOffset(res);
        return FoodBulkResult.parse(res.data ?? const <String, dynamic>{});
      });

  // ---- admin / HR (not cached: lists people) -----------------------------

  Future<FoodDailyReport> daily(String date) => guardApi<FoodDailyReport>(() async {
        final res = await _dio.get<Map<String, dynamic>>(
          '/admin/food/daily',
          queryParameters: {'date': date},
        );
        return FoodDailyReport.fromJson(res.data ?? const <String, dynamic>{});
      });

  /// Downloads the food workbook for one date through the authenticated Dio
  /// client (bearer + refresh) and returns the bytes with a safe file name.
  Future<({Uint8List bytes, String filename})> exportXlsx(String date) =>
      guardApi(() async {
        final res = await _dio.get<List<int>>(
          '/admin/export',
          queryParameters: {'type': 'food', 'from': date, 'to': date},
          options: Options(responseType: ResponseType.bytes),
        );
        final bytes = Uint8List.fromList(res.data ?? const <int>[]);
        return (
          bytes: bytes,
          filename: safeXlsxName(res.headers.value('content-disposition'), date),
        );
      });
}

/// File name from `Content-Disposition`, reduced to `[A-Za-z0-9_.-]` and
/// forced to end in `.xlsx` (never trust a server path).
String safeXlsxName(String? disposition, String date) {
  final fallback = 'employee-connect-food-$date.xlsx';
  if (disposition == null) return fallback;
  final m = RegExp('filename="?([^";]+)"?').firstMatch(disposition);
  var n = m?[1] ?? '';
  n = n.split(RegExp(r'[\\/]')).last.replaceAll(RegExp(r'[^A-Za-z0-9_.-]'), '_');
  if (n.isEmpty || n.startsWith('.')) return fallback;
  return n.toLowerCase().endsWith('.xlsx') ? n : '$n.xlsx';
}

final foodRepositoryProvider = Provider<FoodRepository>(
  (ref) => FoodRepository(
    ref.watch(dioProvider),
    ref.watch(jsonCacheProvider),
    scope: ref.watch(cacheScopeProvider),
  ),
);

// ---- exported files --------------------------------------------------------

/// App-private folder (inside the cache dir, no extra plugin) for downloaded
/// workbooks. They contain employee names, so the folder is wiped on logout
/// and on lock, like the JSON cache.
class ExportStore {
  /// [extraDirs] are folders written by other plugins with copies of our files
  /// (share_plus copies every shared file to `<cache>/share_plus`); they are
  /// wiped together with [dir]. Review 08 M1.
  ExportStore(this.dir, {this.extraDirs = const []});
  final Directory dir;
  final List<Directory> extraDirs;

  Future<File> save(Uint8List bytes, String filename) async {
    final f = reserve(filename);
    await f.writeAsBytes(bytes, flush: true);
    return f;
  }

  /// An (empty, not yet created) file inside the private folder for a caller
  /// that streams into it. Creates the folder.
  File reserve(String filename) {
    if (!dir.existsSync()) dir.createSync(recursive: true);
    return File('${dir.path}${Platform.pathSeparator}$filename');
  }

  /// Deletes only the plugin copies (see [extraDirs]).
  Future<void> clearShareCopies() async {
    for (final d in extraDirs) {
      _deleteDir(d);
    }
  }

  /// Deletes our folder and the plugin copies.
  Future<void> clear() async {
    _deleteDir(dir);
    await clearShareCopies();
  }

  static void _deleteDir(Directory d) {
    try {
      if (d.existsSync()) d.deleteSync(recursive: true);
    } on Object {
      // best effort
    }
  }
}

/// Folder for downloaded files (Excel exports, announcement PDFs). `main.dart`
/// overrides it with `<getTemporaryDirectory()>/ec_exports` (path_provider, the
/// app's private cache); the systemTemp default is only for tests. Review 08 L6.
final exportDirProvider = Provider<Directory>(
  (ref) => Directory('${Directory.systemTemp.path}${Platform.pathSeparator}ec_exports'),
);

/// Where share_plus puts its copy of a shared file: `share_plus` next to the
/// export folder (both inside the cache dir). Review 08 M1.
final shareCopiesDirProvider = Provider<Directory>((ref) {
  final parent = ref.watch(exportDirProvider).parent.path;
  return Directory('$parent${Platform.pathSeparator}share_plus');
});

/// True once the user agreed to hand files to other apps in this session
/// (reset on logout and lock). Review 08 L7.
final shareConfirmedProvider = StateProvider<bool>((ref) => false);

/// Review fix: files left over from an earlier session (the app was killed
/// before logout or lock could clean up) are deleted the first time the store
/// is created. `EmployeeConnectApp` reads this provider at start-up, so the
/// sweep and the logout / lock wipe listener are always active, not only after
/// the first export.
final exportStoreProvider = Provider<ExportStore>((ref) {
  final s = ExportStore(
    ref.watch(exportDirProvider),
    extraDirs: [ref.watch(shareCopiesDirProvider)],
  );
  unawaited(s.clear()); // sweep at start (synchronous file ops, no awaits)
  ref.listen<AuthState>(authControllerProvider, (_, next) {
    if (next is AuthUnauthenticated || next is AuthLocked) {
      ref.read(shareConfirmedProvider.notifier).state = false;
      unawaited(s.clear());
    }
  });
  return s;
});

/// What happened when a saved file was handed to the OS.
enum OpenResult {
  /// The user picked a share target (or the platform cannot tell but the sheet
  /// was shown).
  handedOver,

  /// The user closed the share sheet without choosing a target.
  dismissed,

  /// Sharing is not possible on this device / it failed.
  unavailable,
}

/// Hands a saved file to another app. The default implementation cannot share
/// anything; `main.dart` overrides [fileOpenerProvider] with the share_plus
/// implementation.
abstract class FileOpener {
  Future<OpenResult> open(String path);
}

class NoopFileOpener implements FileOpener {
  const NoopFileOpener();
  @override
  Future<OpenResult> open(String path) async => OpenResult.unavailable;
}

final fileOpenerProvider = Provider<FileOpener>((ref) => const NoopFileOpener());
