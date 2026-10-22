import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Cached company branding (name + logo bytes). Not secret, but it lives in
/// secure storage next to the server URL so one wipe rule covers everything.
class BrandingData {
  const BrandingData({this.name, this.logoVersion = 0, this.logo});
  final String? name;
  final int logoVersion;
  final Uint8List? logo;

  static const empty = BrandingData();
  bool get isEmpty => name == null && logo == null;
}

/// Local state that must survive a wipe (server URL, branding) plus the
/// timestamp of the last successful authenticated call.
abstract class AppStateStore {
  Future<String?> readServerUrl();
  Future<void> writeServerUrl(String? url);
  Future<BrandingData> readBranding();
  Future<void> writeBranding(BrandingData data);

  /// `auth.offline_cache_max_hours` from the last `/config` (0 = off).
  Future<int?> readOfflineMaxHours();
  Future<void> writeOfflineMaxHours(int hours);

  /// Removes everything except the server URL and branding. With
  /// [keepServer] false those go too.
  Future<void> wipe({required bool keepServer});
}

class SecureAppStateStore implements AppStateStore {
  SecureAppStateStore([FlutterSecureStorage? storage])
      : _s = storage ??
            const FlutterSecureStorage(
              aOptions: AndroidOptions(encryptedSharedPreferences: true),
              iOptions: IOSOptions(
                accessibility: KeychainAccessibility.first_unlock_this_device,
              ),
            );
  final FlutterSecureStorage _s;

  static const kServer = 'server.base_url';
  static const kBrandName = 'server.brand_name';
  static const kBrandVersion = 'server.brand_logo_version';
  static const kBrandLogo = 'server.brand_logo_b64';
  static bool isKept(String key) => key.startsWith('server.');

  @override
  Future<String?> readServerUrl() => _s.read(key: kServer);

  @override
  Future<void> writeServerUrl(String? url) => url == null
      ? _s.delete(key: kServer)
      : _s.write(key: kServer, value: url);

  @override
  Future<BrandingData> readBranding() async {
    final name = await _s.read(key: kBrandName);
    final v = int.tryParse(await _s.read(key: kBrandVersion) ?? '') ?? 0;
    final b64 = await _s.read(key: kBrandLogo);
    Uint8List? logo;
    if (b64 != null && b64.isNotEmpty) {
      try {
        logo = base64Decode(b64);
      } on FormatException {
        logo = null;
      }
    }
    return BrandingData(name: name, logoVersion: v, logo: logo);
  }

  @override
  Future<void> writeBranding(BrandingData d) async {
    Future<void> put(String k, String? v) =>
        v == null ? _s.delete(key: k) : _s.write(key: k, value: v);
    await put(kBrandName, d.name);
    await put(kBrandVersion, d.logoVersion.toString());
    await put(kBrandLogo, d.logo == null ? null : base64Encode(d.logo!));
  }

  static const kOffline = 'auth.offline_max_hours';

  @override
  Future<int?> readOfflineMaxHours() async =>
      int.tryParse(await _s.read(key: kOffline) ?? '');

  @override
  Future<void> writeOfflineMaxHours(int hours) =>
      _s.write(key: kOffline, value: '$hours');

  @override
  Future<void> wipe({required bool keepServer}) async {
    final all = await _s.readAll();
    for (final k in all.keys) {
      if (keepServer && isKept(k)) continue;
      await _s.delete(key: k);
    }
  }
}

class MemoryAppStateStore implements AppStateStore {
  String? url;
  BrandingData branding = BrandingData.empty;
  final Map<String, String> other = {};

  @override
  Future<String?> readServerUrl() async => url;
  @override
  Future<void> writeServerUrl(String? u) async => url = u;
  @override
  Future<BrandingData> readBranding() async => branding;
  @override
  Future<void> writeBranding(BrandingData data) async => branding = data;
  int? offlineHours;
  @override
  Future<int?> readOfflineMaxHours() async => offlineHours;
  @override
  Future<void> writeOfflineMaxHours(int hours) async => offlineHours = hours;
  @override
  Future<void> wipe({required bool keepServer}) async {
    offlineHours = null;
    other.clear();
    if (!keepServer) {
      url = null;
      branding = BrandingData.empty;
    }
  }
}

final appStateStoreProvider = Provider<AppStateStore>((ref) => SecureAppStateStore());
