import 'dart:math';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Tokens and the biometric flag live ONLY here (Keystore / Keychain).
abstract class TokenStore {
  Future<String?> readAccess();
  Future<String?> readRefresh();
  Future<void> saveTokens({required String access, required String refresh});
  Future<bool> readBiometricEnabled();
  Future<void> writeBiometricEnabled({required bool enabled});
  Future<String> deviceId();

  /// Removes tokens and the biometric flag (device id is kept).
  Future<void> clearSession();

  Future<bool> hasSession() async => (await readRefresh()) != null;

  /// When an authenticated call last succeeded (offline cache limit,
  /// `auth.offline_cache_max_hours`). Persisted by [SecureTokenStore].
  DateTime? _lastOk;
  Future<DateTime?> readLastOk() async => _lastOk;
  Future<void> writeLastOk(DateTime at) async => _lastOk = at;
}

final tokenStoreProvider = Provider<TokenStore>((ref) => SecureTokenStore());

class SecureTokenStore extends TokenStore {
  SecureTokenStore([FlutterSecureStorage? storage])
      : _s = storage ??
            const FlutterSecureStorage(
              aOptions: AndroidOptions(encryptedSharedPreferences: true),
              // L5: never migrate to another device via backup / restore.
              iOptions: IOSOptions(
                accessibility: KeychainAccessibility.first_unlock_this_device,
              ),
            );

  static const _kAccess = 'access_token';
  static const _kRefresh = 'refresh_token';
  static const _kBio = 'biometric_enabled';
  static const _kDevice = 'device_id';
  static const _kLastOk = 'last_ok_at';

  final FlutterSecureStorage _s;
  String? _accessCache; // avoid a Keystore round trip on every request
  String? _deviceCache;

  @override
  Future<String?> readAccess() async => _accessCache ??= await _s.read(key: _kAccess);

  @override
  Future<String?> readRefresh() => _s.read(key: _kRefresh);

  @override
  Future<void> saveTokens({required String access, required String refresh}) async {
    _accessCache = access;
    await _s.write(key: _kAccess, value: access);
    await _s.write(key: _kRefresh, value: refresh);
  }

  @override
  Future<bool> readBiometricEnabled() async =>
      (await _s.read(key: _kBio)) == '1';

  @override
  Future<void> writeBiometricEnabled({required bool enabled}) => enabled
      ? _s.write(key: _kBio, value: '1')
      : _s.delete(key: _kBio);

  @override
  Future<String> deviceId() async {
    final cached = _deviceCache;
    if (cached != null) return cached;
    var id = await _s.read(key: _kDevice);
    if (id == null) {
      final r = Random.secure();
      id = List.generate(16, (_) => r.nextInt(256).toRadixString(16).padLeft(2, '0'))
          .join();
      await _s.write(key: _kDevice, value: id);
    }
    return _deviceCache = id;
  }

  @override
  Future<DateTime?> readLastOk() async {
    final ms = int.tryParse(await _s.read(key: _kLastOk) ?? '');
    return ms == null ? null : DateTime.fromMillisecondsSinceEpoch(ms);
  }

  @override
  Future<void> writeLastOk(DateTime at) =>
      _s.write(key: _kLastOk, value: '${at.millisecondsSinceEpoch}');

  @override
  Future<void> clearSession() async {
    _accessCache = null;
    await _s.delete(key: _kLastOk);
    await _s.delete(key: _kAccess);
    await _s.delete(key: _kRefresh);
    await _s.delete(key: _kBio);
  }
}
