import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/admin/data/admin_repository.dart';
import 'package:employee_connect/features/devices/data/device_repository.dart';
import 'package:employee_connect/features/devices/domain/device_models.dart';
import 'package:flutter_test/flutter_test.dart';

import 'device_logic_test.dart' show deviceJson;

/// Paths, bodies and parsing of the section 21 calls against a stubbed transport.
class Call {
  Call(this.method, this.path, this.body, this.query);
  final String method;
  final String path;
  final Object? body;
  final Map<String, dynamic> query;
}

class _Adapter implements HttpClientAdapter {
  _Adapter(this.reply);
  final (int, Object?) Function(Call call) reply;
  final List<Call> calls = [];

  @override
  Future<ResponseBody> fetch(RequestOptions options, Stream<Uint8List>? requestStream, Future<void>? cancelFuture) async {
    Object? body = options.data;
    if (body is String && body.isNotEmpty) body = jsonDecode(body);
    final c = Call(options.method, options.path, body, options.queryParameters);
    calls.add(c);
    final (status, json) = reply(c);
    return ResponseBody.fromString(
      json == null ? '' : jsonEncode(json),
      status,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}

(Dio, _Adapter) _dio(Object? Function(Call) reply, {int status = 200}) {
  final a = _Adapter((c) => (status, reply(c)));
  return (Dio(BaseOptions(baseUrl: 'http://x/api'))..httpClientAdapter = a, a);
}

void main() {
  test('lists devices and fetches one with recent syncs', () async {
    final (dio, a) = _dio((c) => c.path.endsWith('/devices')
        ? {'items': [deviceJson()]}
        : deviceJson(extra: {'recent_syncs': [{'id': 's', 'kind': 'manual', 'status': 'ok'}]}),);
    final repo = DeviceRepository(dio);
    expect((await repo.devices()).single.name, 'Main entrance');
    expect((await repo.device('d1')).recentSyncs, hasLength(1));
    expect(a.calls.map((c) => '${c.method} ${c.path}'), ['GET /admin/devices', 'GET /admin/devices/d1']);
  });

  test('create and update send the key only as the form says', () async {
    final (dio, a) = _dio((c) => deviceJson());
    final repo = DeviceRepository(dio);
    await repo.createDevice({'name': 'A', 'comm_key': 1234});
    await repo.updateDevice('d1', {'name': 'A'});
    expect(a.calls[0].method, 'POST');
    expect((a.calls[0].body! as Map)['comm_key'], 1234);
    expect(a.calls[1].method, 'PUT');
    expect(a.calls[1].path, '/admin/devices/d1');
    expect((a.calls[1].body! as Map).containsKey('comm_key'), isFalse);
  });

  test('delete, accept identity, test and sync paths', () async {
    final (dio, a) = _dio((c) {
      if (c.path.endsWith('/accept-identity')) return deviceJson();
      if (c.path.endsWith('/sync-all')) {
        return {
          'items': [
            {'device_id': 'd1', 'sync_id': 's1'},
            {'device_id': 'd2', 'sync_id': 's2'},
          ],
        };
      }
      if (c.method == 'DELETE') return null;
      return {'sync_id': 'job1'};
    });
    final repo = DeviceRepository(dio);
    await repo.deleteDevice('d1');
    await repo.acceptIdentity('d1');
    expect(await repo.testDevice('d1'), 'job1');
    expect(await repo.testSettings(ip: '10.0.0.5', port: 4370, transport: 'tcp', commKey: 77), 'job1');
    expect(await repo.syncNow('d1'), 'job1');
    expect((await repo.syncAll()).map((e) => e.syncId), ['s1', 's2']);
    expect(
      a.calls.map((c) => '${c.method} ${c.path}'),
      [
        'DELETE /admin/devices/d1',
        'POST /admin/devices/d1/accept-identity',
        'POST /admin/devices/d1/test',
        'POST /admin/devices/test',
        'POST /admin/devices/d1/sync',
        'POST /admin/devices/sync-all',
      ],
    );
    expect(a.calls[3].body, {'ip': '10.0.0.5', 'port': 4370, 'transport': 'tcp', 'comm_key': 77});
  });

  test('device users and mapping', () async {
    final (dio, a) = _dio((c) {
      if (c.method == 'GET') {
        return {
          'items': [{'id': 'u1', 'device_user_id': '101', 'status': 'unmatched'}],
          'page': 1,
          'page_size': 20,
          'total': 1,
        };
      }
      return {
        'items': [{'id': 'u1', 'device_user_id': '101', 'status': 'matched'}],
        'recompute_queued': true,
      };
    });
    final repo = DeviceRepository(dio);
    final page = await repo.deviceUsers(const DeviceUserQuery(deviceId: 'd1', q: ' 10 '));
    expect(page.items.single.deviceUserId, '101');
    expect(a.calls[0].query['status'], 'unmatched');
    expect(a.calls[0].query['device_id'], 'd1');
    expect(a.calls[0].query['q'], '10');
    final m = await repo.mapUser('u1', userId: 'e1', allDevices: true);
    expect(m.recomputeQueued, isTrue);
    expect(a.calls[1].body, {'user_id': 'e1', 'apply_to_all_devices': true});
    await repo.mapUser('u1', userId: null, allDevices: false);
    expect((a.calls[2].body! as Map)['user_id'], isNull);
    await repo.resetToAuto('u1');
    expect(a.calls[3].path, '/admin/device-users/u1/auto');
  });

  test('punch log sends the filters', () async {
    final (dio, a) = _dio((c) => {
          'items': [
            {'id': '9', 'device_user_id': '101', 'outcome': 'quarantined', 'reason': 'future'},
          ],
          'total': 1,
        },);
    final page = await DeviceRepository(dio).punches(
      const PunchQuery(from: '2026-11-01', to: '2026-11-10', deviceId: 'd1', employeeId: 'e1', deviceUserId: '101', outcome: 'quarantined'),
    );
    expect(page.items.single.outcome, 'quarantined');
    expect(a.calls.single.query, containsPair('outcome', 'quarantined'));
    expect(a.calls.single.query, containsPair('employee_id', 'e1'));
    expect(a.calls.single.query, containsPair('from', '2026-11-01'));
  });

  test('my punches query', () async {
    final (dio, a) = _dio((c) => {
          'items': [
            {'punch_at': '2026-11-10T03:32:00Z', 'device_name': 'Main', 'method': 'card', 'used_as': 'check_in'},
          ],
        },);
    final list = await DeviceRepository(dio).myPunches(date: '2026-11-10');
    expect(list.single.deviceName, 'Main');
    expect(a.calls.single.path, '/attendance/punches');
    expect(a.calls.single.query, {'date': '2026-11-10'});
  });

  test('use-device posts the reason', () async {
    final (dio, a) = _dio((c) => {'work_date': '2026-11-10', 'record': null, 'device_punches': <Object>[]});
    final d = await AdminRepository(dio).useDevicePunches('u1', '2026-11-10', reason: '  Forgot  ');
    expect(d.workDate, '2026-11-10');
    expect(a.calls.single.path, '/admin/attendance/u1/2026-11-10/use-device');
    expect(a.calls.single.body, {'reason': 'Forgot'});
  });

  group('job polling', () {
    test('polls until the job is finished', () async {
      var n = 0;
      final (dio, a) = _dio((c) {
        n++;
        return {'id': 'j', 'kind': 'manual', 'status': n < 3 ? 'running' : 'ok', 'fetched': 1, 'inserted': 1};
      });
      final waits = <Duration>[];
      final job = await DeviceRepository(dio).waitForSync('j', wait: (d) async => waits.add(d));
      expect(job.succeeded, isTrue);
      expect(a.calls, hasLength(3));
      expect(waits, [const Duration(seconds: 2), const Duration(seconds: 2)]);
      expect(a.calls.first.path, '/admin/device-syncs/j');
    });

    test('a failed job returns its error code', () async {
      final (dio, _) = _dio((c) => {'id': 'j', 'kind': 'test', 'status': 'failed', 'error_code': 'DEVICE_BUSY'});
      final job = await DeviceRepository(dio).waitForSync('j');
      expect(job.succeeded, isFalse);
      expect(job.summary, contains('Another program'));
    });

    test('gives up after two minutes', () async {
      final (dio, a) = _dio((c) => {'id': 'j', 'kind': 'manual', 'status': 'queued'});
      await expectLater(
        DeviceRepository(dio).waitForSync('j', wait: (_) async {}),
        throwsA(isA<SyncPollTimeout>()),
      );
      expect(a.calls.length, 61); // 0 s, 2 s ... 120 s
    });

    test('stops when cancelled', () async {
      final (dio, a) = _dio((c) => {'id': 'j', 'kind': 'manual', 'status': 'running'});
      final job = await DeviceRepository(dio).waitForSync('j', cancelled: () => true);
      expect(job.isFinished, isFalse);
      expect(a.calls, hasLength(1));
    });
  });

  test('HTTP errors become ApiException with the contract code', () async {
    final (dio, _) = _dio(
      (c) => {
        'error': {'code': 'DEVICE_DISABLED', 'message': 'disabled'},
      },
      status: 409,
    );
    await expectLater(
      DeviceRepository(dio).syncNow('d1'),
      throwsA(isA<ApiException>().having((e) => e.code, 'code', 'DEVICE_DISABLED')),
    );
  });
}
