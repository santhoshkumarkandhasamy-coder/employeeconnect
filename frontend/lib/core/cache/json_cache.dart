import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../features/auth/presentation/auth_controller.dart';
import '../../features/auth/presentation/auth_state.dart';
import '../api/api_exception.dart';

/// Small JSON key/value cache with a TTL. Deliberately NOT for secrets
/// (tokens stay in secure storage); used for offline-friendly reads of
/// history / calendar / late data.
///
/// Sensitive attendance data (review 02, L13). Choice: the cache is plain JSON
/// in the app-private cache dir, so we (1) never write location/proximity
/// fields into it: [fetchWithCache] runs every payload through
/// [scrubSensitive] (distance_m, radius_m, within_fence, selfie_url, lat/lng,
/// accuracy_m, ...), and (2) wipe it on logout AND when the app locks after
/// inactivity, so nothing is readable on disk while the app is locked. We did
/// not add file encryption: that would need a key in secure storage and a new
/// dependency for data that is already minimised. The trade-off is that
/// offline history shows times and statuses but no distance or selfie link,
/// and the first read after an unlock needs the network.
abstract class JsonCache {
  /// Returns the stored value, or null when missing, expired or unreadable.
  Future<Object?> read(String key, {Duration? maxAge});
  Future<void> write(String key, Object value);
  Future<void> clear();
}

class MemoryJsonCache implements JsonCache {
  MemoryJsonCache({DateTime Function()? now}) : _now = now ?? DateTime.now;
  final DateTime Function() _now;
  final Map<String, (DateTime, Object)> _m = {};

  @override
  Future<Object?> read(String key, {Duration? maxAge}) async {
    final e = _m[key];
    if (e == null) return null;
    if (maxAge != null && _now().difference(e.$1) > maxAge) return null;
    return e.$2;
  }

  @override
  Future<void> write(String key, Object value) async => _m[key] = (_now(), value);

  @override
  Future<void> clear() async => _m.clear();
}

/// File-backed cache stored in the OS temp/cache directory (no extra plugin).
/// The OS may evict it at any time, which is fine for a cache.
class FileJsonCache implements JsonCache {
  FileJsonCache(this.dir, {DateTime Function()? now}) : _now = now ?? DateTime.now;
  final Directory dir;
  final DateTime Function() _now;

  File _file(String key) => File(
        '${dir.path}${Platform.pathSeparator}'
        '${key.replaceAll(RegExp(r'[^A-Za-z0-9_.-]'), '_')}.json',
      );

  @override
  Future<Object?> read(String key, {Duration? maxAge}) async {
    try {
      final f = _file(key);
      if (!f.existsSync()) return null;
      final m = jsonDecode(await f.readAsString()) as Map<String, dynamic>;
      final at = DateTime.fromMillisecondsSinceEpoch(m['t'] as int);
      if (maxAge != null && _now().difference(at) > maxAge) return null;
      return m['d'] as Object?;
    } on Object {
      return null;
    }
  }

  @override
  Future<void> write(String key, Object value) async {
    try {
      if (!dir.existsSync()) dir.createSync(recursive: true);
      await _file(key).writeAsString(
        jsonEncode({'t': _now().millisecondsSinceEpoch, 'd': value}),
        flush: true,
      );
    } on Object {
      // cache is best effort
    }
  }

  @override
  Future<void> clear() async {
    try {
      if (dir.existsSync()) dir.deleteSync(recursive: true);
    } on Object {
      // best effort
    }
  }
}

/// Keys that must never be persisted in the offline cache.
const Set<String> sensitiveCacheKeys = {
  'distance_m',
  'radius_m',
  'within_fence',
  'selfie_url',
  'selfie_file_id',
  'lat',
  'lng',
  'accuracy_m',
  'check_in_loc',
  'check_out_loc',
  'is_mock_location',
  // Leave: free-text reasons/comments can be medical or personal, and the
  // attachment link grants access to a document. Offline lists show type,
  // dates, days and status only.
  'reason',
  'decision_comment',
  'attachment_url',
  // Profile (module 07): personal data stays out of the offline cache. Only
  // name, department, role and official email are kept.
  'dob',
  'wedding_anniversary',
  'personal_email',
  'phone',
  'emergency_contact',
  'blood_group',
  'address',
  // Announcements and home (review 07 L8): bodies can be confidential and the
  // celebrations list carries colleagues' names and birthdays. Offline lists
  // keep titles and dates only; celebrations are not shown offline.
  'body',
  'celebrations',
};

/// Deep copy of [json] without [sensitiveCacheKeys].
Object scrubSensitive(Object json) {
  Object? walk(Object? v) {
    if (v is Map) {
      return <String, dynamic>{
        for (final e in v.entries)
          if (!sensitiveCacheKeys.contains(e.key)) '${e.key}': walk(e.value),
      };
    }
    if (v is List) return <dynamic>[for (final e in v) walk(e)];
    return v;
  }

  return walk(json)!;
}

final jsonCacheProvider = Provider<JsonCache>((ref) {
  final cache = FileJsonCache(
    Directory('${Directory.systemTemp.path}${Platform.pathSeparator}ec_cache'),
  );
  // Start of the app: drop anything a crash or a killed process left on disk
  // (the logout / lock wipe below never ran in that case). Review 07 L8.
  unawaited(cache.clear());
  ref.listen<AuthState>(authControllerProvider, (_, next) {
    // Logout or inactivity lock: nothing stays on disk while signed out/locked.
    if (next is AuthUnauthenticated || next is AuthLocked) cache.clear();
  });
  return cache;
});

/// Default freshness window for cached reads.
const Duration cacheTtl = Duration(hours: 24);

/// A parsed value plus whether it came from the offline cache.
class Cached<T> {
  const Cached(this.value, {this.stale = false});
  final T value;

  /// True when the network failed and this is the last cached copy.
  final bool stale;
}

/// Fetches JSON from the network and caches it; on a *network* failure falls
/// back to the cached copy (within [cacheTtl]). Other API errors propagate.
Future<Cached<T>> fetchWithCache<T>({
  required JsonCache cache,
  required String key,
  required Future<Object> Function() fetch,
  required T Function(Object json) parse,
}) async {
  try {
    final json = await fetch();
    final value = parse(json);
    await cache.write(key, scrubSensitive(json));
    return Cached<T>(value);
  } on ApiException catch (e) {
    if (!e.isNetwork) rethrow;
    final hit = await cache.read(key, maxAge: cacheTtl);
    if (hit == null) rethrow;
    final T value;
    try {
      value = parse(hit);
    } on Object {
      throw e;
    }
    return Cached<T>(value, stale: true);
  }
}
