import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/dio_client.dart';
import '../../attendance/domain/attendance_models.dart' show Paged;
import '../domain/device_models.dart';

/// Raised when a job is still not finished after the polling time (2 minutes).
class SyncPollTimeout implements Exception {
  const SyncPollTimeout();
  @override
  String toString() => 'SyncPollTimeout';
}

/// api-contract.md section 21. No caching: device data is admin-only and kept in memory.
class DeviceRepository {
  DeviceRepository(this._dio);
  final Dio _dio;

  Future<Map<String, dynamic>> _get(String path, [Map<String, Object?>? query]) => guardApi(() async {
        final res = await _dio.get<Map<String, dynamic>>(path, queryParameters: query);
        return res.data ?? const <String, dynamic>{};
      });

  Future<Map<String, dynamic>> _send(String method, String path, [Object? body]) => guardApi(() async {
        final res = await _dio.request<Object?>(path, data: body, options: Options(method: method));
        final d = res.data;
        return d is Map ? Map<String, dynamic>.from(d) : const <String, dynamic>{};
      });

  // ---- devices ----------------------------------------------------------------

  Future<List<Device>> devices() async {
    final j = await _get('/admin/devices');
    return [
      if (j['items'] is List)
        for (final e in j['items'] as List)
          if (e is Map<String, dynamic>) Device.fromJson(e),
    ];
  }

  Future<Device> device(String id) async => Device.fromJson(await _get('/admin/devices/$id'));

  Future<Device> createDevice(Map<String, Object?> body) async =>
      Device.fromJson(await _send('POST', '/admin/devices', body));

  Future<Device> updateDevice(String id, Map<String, Object?> body) async =>
      Device.fromJson(await _send('PUT', '/admin/devices/$id', body));

  Future<void> deleteDevice(String id) async {
    await _send('DELETE', '/admin/devices/$id');
  }

  Future<Device> acceptIdentity(String id) async =>
      Device.fromJson(await _send('POST', '/admin/devices/$id/accept-identity', const <String, Object?>{}));

  Future<Paged<DeviceSync>> syncs(String id, {int page = 1}) async => Paged.parse(
        await _get('/admin/devices/$id/syncs', {'page': page, 'page_size': 20}),
        DeviceSync.fromJson,
      );

  // ---- jobs -------------------------------------------------------------------

  /// Test saved device. Returns the job id.
  Future<String> testDevice(String id) async => '${(await _send('POST', '/admin/devices/$id/test'))['sync_id']}';

  /// Test unsaved settings. [commKey] is null when none is set; it is sent once and never kept.
  Future<String> testSettings({
    required String ip,
    required int port,
    required String transport,
    int? commKey,
  }) async =>
      '${(await _send('POST', '/admin/devices/test', {
        'ip': ip,
        'port': port,
        'transport': transport,
        'comm_key': commKey,
      }))['sync_id']}';

  Future<String> syncNow(String id) async => '${(await _send('POST', '/admin/devices/$id/sync'))['sync_id']}';

  Future<List<({String deviceId, String syncId})>> syncAll() async {
    final j = await _send('POST', '/admin/devices/sync-all');
    return [
      if (j['items'] is List)
        for (final e in j['items'] as List)
          if (e is Map) (deviceId: '${e['device_id']}', syncId: '${e['sync_id']}'),
    ];
  }

  Future<DeviceSync> syncJob(String syncId) async =>
      DeviceSync.fromJson(await _get('/admin/device-syncs/$syncId'));

  /// Polls the job every [interval] until it is finished; throws [SyncPollTimeout] after [timeout].
  /// [wait] and [cancelled] exist for tests and for screens that were closed.
  Future<DeviceSync> waitForSync(
    String syncId, {
    Duration interval = const Duration(seconds: 2),
    Duration timeout = const Duration(minutes: 2),
    Future<void> Function(Duration)? wait,
    bool Function()? cancelled,
  }) async {
    final pause = wait ?? (d) => Future<void>.delayed(d);
    var waited = Duration.zero;
    while (true) {
      final job = await syncJob(syncId);
      if (job.isFinished) return job;
      if (cancelled?.call() ?? false) return job;
      if (waited >= timeout) throw const SyncPollTimeout();
      await pause(interval);
      waited += interval;
    }
  }

  // ---- device users -------------------------------------------------------------

  Future<Paged<DeviceUser>> deviceUsers(DeviceUserQuery q, {int page = 1}) async => Paged.parse(
        await _get('/admin/device-users', {...q.toQuery(), 'page': page, 'page_size': 20}),
        DeviceUser.fromJson,
      );

  /// [userId] null = ignore this device user.
  Future<MappingResult> mapUser(String id, {required String? userId, required bool allDevices}) async =>
      MappingResult.fromJson(await _send('PUT', '/admin/device-users/$id/mapping', {
        'user_id': userId,
        'apply_to_all_devices': allDevices,
      }),);

  Future<MappingResult> resetToAuto(String id) async =>
      MappingResult.fromJson(await _send('POST', '/admin/device-users/$id/auto', const <String, Object?>{}));

  // ---- punch log ------------------------------------------------------------------

  Future<Paged<DevicePunch>> punches(PunchQuery q, {int page = 1}) async => Paged.parse(
        await _get('/admin/device-punches', {...q.toQuery(), 'page': page, 'page_size': 30}),
        DevicePunch.fromJson,
      );

  // ---- employee view ---------------------------------------------------------------

  Future<List<MyPunch>> myPunches({String? date, String? from, String? to}) async {
    final j = await _get('/attendance/punches', {
      if (date != null) 'date': date,
      if (from != null) 'from': from,
      if (to != null) 'to': to,
    });
    return [
      if (j['items'] is List)
        for (final e in j['items'] as List)
          if (e is Map<String, dynamic>) MyPunch.fromJson(e),
    ];
  }
}

final deviceRepositoryProvider = Provider<DeviceRepository>((ref) => DeviceRepository(ref.watch(dioProvider)));
