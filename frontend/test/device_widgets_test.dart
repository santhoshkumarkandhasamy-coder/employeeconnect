import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/admin/data/admin_repository.dart';
import 'package:employee_connect/features/admin/domain/admin_models.dart';
import 'package:employee_connect/features/admin/presentation/admin_widgets.dart';
import 'package:employee_connect/features/admin/presentation/manual_attendance_screen.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart';
import 'package:employee_connect/features/devices/data/device_repository.dart';
import 'package:employee_connect/features/devices/domain/device_models.dart';
import 'package:employee_connect/features/devices/presentation/device_detail_screen.dart';
import 'package:employee_connect/features/devices/presentation/device_form_screen.dart';
import 'package:employee_connect/features/devices/presentation/device_providers.dart';
import 'package:employee_connect/features/devices/presentation/device_users_tab.dart';
import 'package:employee_connect/features/devices/presentation/devices_home_screen.dart';
import 'package:employee_connect/features/devices/presentation/my_punches.dart';
import 'package:employee_connect/features/devices/presentation/privacy_screen.dart';
import 'package:employee_connect/features/devices/presentation/punch_log_tab.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'device_logic_test.dart' show deviceJson;
import 'helpers/module_fakes.dart';

class FakeDeviceRepo extends DeviceRepository {
  FakeDeviceRepo() : super(Dio());

  List<Device> list = [Device.fromJson(deviceJson())];
  Device? detail;
  List<DeviceUser> users = [];
  List<DevicePunch> punchLog = [];
  List<MyPunch> mine = [];
  DeviceSync job = const DeviceSync(id: 'j1', kind: 'manual', status: 'ok', fetched: 12, inserted: 3);
  Object? listError;
  Object? syncError;
  final List<String> calls = [];
  Map<String, Object?>? lastBody;

  @override
  Future<List<Device>> devices() async {
    calls.add('devices');
    if (listError != null) throw listError!;
    return list;
  }

  @override
  Future<Device> device(String id) async => detail ?? list.firstWhere((d) => d.id == id);

  @override
  Future<Device> createDevice(Map<String, Object?> body) async {
    calls.add('create');
    lastBody = body;
    return list.first;
  }

  @override
  Future<Device> updateDevice(String id, Map<String, Object?> body) async {
    calls.add('update $id');
    lastBody = body;
    return list.first;
  }

  @override
  Future<Device> acceptIdentity(String id) async {
    calls.add('accept $id');
    return list.first;
  }

  @override
  Future<String> syncNow(String id) async {
    calls.add('sync $id');
    if (syncError != null) throw syncError!;
    return 'j1';
  }

  @override
  Future<List<({String deviceId, String syncId})>> syncAll() async {
    calls.add('syncAll');
    return [(deviceId: 'd1', syncId: 's1')];
  }

  @override
  Future<String> testDevice(String id) async {
    calls.add('test $id');
    return 'j1';
  }

  @override
  Future<String> testSettings({required String ip, required int port, required String transport, int? commKey}) async {
    calls.add('testSettings $ip:$port $transport key=${commKey ?? '-'}');
    return 'j1';
  }

  @override
  Future<DeviceSync> waitForSync(
    String syncId, {
    Duration interval = const Duration(seconds: 2),
    Duration timeout = const Duration(minutes: 2),
    Future<void> Function(Duration)? wait,
    bool Function()? cancelled,
  }) async =>
      job;

  @override
  Future<Paged<DeviceUser>> deviceUsers(DeviceUserQuery q, {int page = 1}) async {
    calls.add('users ${q.status}');
    return Paged(items: users, page: 1, total: users.length, pageSize: 20);
  }

  @override
  Future<MappingResult> mapUser(String id, {required String? userId, required bool allDevices}) async {
    calls.add('map $id ${userId ?? 'ignore'} all=$allDevices');
    return const MappingResult(items: [], recomputeQueued: true);
  }

  @override
  Future<MappingResult> resetToAuto(String id) async {
    calls.add('auto $id');
    return const MappingResult(items: []);
  }

  @override
  Future<Paged<DevicePunch>> punches(PunchQuery q, {int page = 1}) async {
    calls.add('punches ${q.from}..${q.to} ${q.outcome ?? 'any'}');
    return Paged(items: punchLog, page: 1, total: punchLog.length, pageSize: 30);
  }

  @override
  Future<List<MyPunch>> myPunches({String? date, String? from, String? to}) async {
    calls.add('mine $date');
    return mine;
  }
}

const _view = ['device.view'];
const _hr = ['device.view', 'device.map'];
const _admin = ['device.view', 'device.map', 'device.manage'];

List<Override> _ov(FakeDeviceRepo repo, List<String> perms, {bool console = false}) => [
      deviceRepositoryProvider.overrideWithValue(repo),
      adminPermissionsProvider.overrideWithValue(perms),
      deviceEditAllowedProvider.overrideWithValue(console),
    ];

DeviceUser _user(String id, {String status = 'unmatched', String method = 'unmatched'}) => DeviceUser.fromJson({
      'id': id,
      'device': {'id': 'd1', 'name': 'Main entrance'},
      'device_user_id': '10$id',
      'name': 'RAVI $id',
      'card_masked': '****4821',
      'status': status,
      'match_method': method,
      'unapplied_punches': 4,
    });

Future<void> _tall(WidgetTester t) async {
  t.view.physicalSize = const Size(900, 1800);
  t.view.devicePixelRatio = 1;
  addTearDown(t.view.resetPhysicalSize);
  addTearDown(t.view.resetDevicePixelRatio);
}

void main() {
  group('home and list', () {
    testWidgets('without device.view nothing is requested', (t) async {
      final repo = FakeDeviceRepo();
      await t.pumpWidget(app(const DevicesHomeScreen(), _ov(repo, const ['device.map'])));
      await t.pumpAndSettle();
      expect(find.text('You do not have permission to view this.'), findsOneWidget);
      expect(repo.calls, isEmpty);
    });

    testWidgets('lists devices with status, last sync and unmatched count; view only has no actions', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo();
      await t.pumpWidget(app(const DevicesHomeScreen(), _ov(repo, _view)));
      await t.pumpAndSettle();
      expect(find.text('Main entrance'), findsOneWidget);
      expect(find.text('OK'), findsOneWidget);
      expect(find.textContaining('192.168.0.201:4370'), findsOneWidget);
      expect(find.textContaining('3 unmatched users'), findsOneWidget);
      expect(find.byKey(const ValueKey('sync-d1')), findsNothing);
      expect(find.byKey(const ValueKey('sync-all')), findsNothing);
      expect(find.byKey(const ValueKey('add-device')), findsNothing);
    });

    testWidgets('shows the plain-language last error of a failing device', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo()
        ..list = [Device.fromJson(deviceJson(status: 'failing', extra: {'last_error_code': 'DEVICE_TIMEOUT'}))];
      await t.pumpWidget(app(const DevicesHomeScreen(), _ov(repo, _view)));
      await t.pumpAndSettle();
      expect(find.text('Failing'), findsOneWidget);
      expect(find.textContaining('did not answer in time'), findsOneWidget);
      expect(find.textContaining('DEVICE_TIMEOUT'), findsNothing);
    });

    testWidgets('empty and error states', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo()..list = [];
      await t.pumpWidget(app(const DevicesHomeScreen(), _ov(repo, _view)));
      await t.pumpAndSettle();
      expect(find.text('No attendance devices have been added yet.'), findsOneWidget);

      final bad = FakeDeviceRepo()..listError = const ApiException(code: 'NETWORK_ERROR', message: 'No connection.');
      await t.pumpWidget(app(const DevicesHomeScreen(), _ov(bad, _view)));
      await t.pumpAndSettle();
      expect(find.text('No connection.'), findsOneWidget);
      expect(find.text('Try again'), findsOneWidget);
    });

    testWidgets('HR can Sync now and sees the result; add is hidden on the phone', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo();
      await t.pumpWidget(app(const DevicesHomeScreen(), _ov(repo, _hr)));
      await t.pumpAndSettle();
      expect(find.byKey(const ValueKey('add-device')), findsNothing);
      await t.tap(find.byKey(const ValueKey('sync-d1')));
      await t.pumpAndSettle();
      expect(repo.calls, contains('sync d1'));
      expect(find.textContaining('Synced: 12 records read, 3 new'), findsOneWidget);
    });

    testWidgets('a failed sync is explained in words', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo()
        ..job = const DeviceSync(id: 'j1', kind: 'manual', status: 'failed', errorCode: 'DEVICE_BUSY');
      await t.pumpWidget(app(const DevicesHomeScreen(), _ov(repo, _hr)));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const ValueKey('sync-d1')));
      await t.pumpAndSettle();
      expect(find.textContaining('Another program is connected'), findsOneWidget);
    });

    testWidgets('a rate limit on Sync now shows the wait', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo()
        ..syncError = const ApiException(code: 'RATE_LIMITED', message: 'x', status: 429, retryAfter: Duration(seconds: 42));
      await t.pumpWidget(app(const DevicesHomeScreen(), _ov(repo, _hr)));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const ValueKey('sync-d1')));
      await t.pumpAndSettle();
      expect(find.textContaining('42 seconds'), findsOneWidget);
    });

    testWidgets('sync all and add device appear for admin in the console only', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo();
      await t.pumpWidget(app(const DevicesHomeScreen(), _ov(repo, _admin)));
      await t.pumpAndSettle();
      expect(find.byKey(const ValueKey('add-device')), findsNothing);
      expect(find.byKey(const ValueKey('sync-all')), findsOneWidget);

      await t.pumpWidget(app(const DevicesHomeScreen(), _ov(repo, _admin, console: true)));
      await t.pumpAndSettle();
      expect(find.byKey(const ValueKey('add-device')), findsOneWidget);
      await t.tap(find.byKey(const ValueKey('sync-all')));
      await t.pumpAndSettle();
      expect(repo.calls, contains('syncAll'));
    });
  });

  group('detail', () {
    Device failing() => Device.fromJson(deviceJson(status: 'failing', extra: {
          'last_error_code': 'DEVICE_IDENTITY_CHANGED',
          'recent_syncs': [
            {'id': 's1', 'kind': 'schedule', 'status': 'failed', 'error_code': 'DEVICE_IDENTITY_CHANGED'},
          ],
        },),);

    testWidgets('identity change can be accepted by admin after confirming', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo()
        ..list = [failing()]
        ..detail = failing();
      await t.pumpWidget(app(const DeviceDetailScreen(deviceId: 'd1'), _ov(repo, _admin)));
      await t.pumpAndSettle();
      expect(find.byKey(const ValueKey('last-error')), findsOneWidget);
      expect(find.textContaining('different machine'), findsWidgets);
      await t.tap(find.byKey(const ValueKey('accept-identity')));
      await t.pumpAndSettle();
      expect(repo.calls, isNot(contains('accept d1')));
      await t.tap(find.text('Accept'));
      await t.pumpAndSettle();
      expect(repo.calls, contains('accept d1'));
    });

    testWidgets('HR cannot accept, test or edit', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo()
        ..list = [failing()]
        ..detail = failing();
      await t.pumpWidget(app(const DeviceDetailScreen(deviceId: 'd1'), _ov(repo, _hr, console: true)));
      await t.pumpAndSettle();
      expect(find.byKey(const ValueKey('accept-identity')), findsNothing);
      expect(find.byKey(const ValueKey('detail-test')), findsNothing);
      expect(find.byKey(const ValueKey('edit-device')), findsNothing);
      expect(find.byKey(const ValueKey('detail-sync')), findsOneWidget);
    });

    testWidgets('test connection shows serial, model and counts', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo()
        ..job = DeviceSync.fromJson({
          'id': 'j1',
          'kind': 'test',
          'status': 'ok',
          'result': {
            'serial_number': 'SN-77',
            'model': 'K40',
            'firmware': 'Ver 6.60',
            'device_time': '2026-11-10T09:02:11',
            'clock_offset_s': 4,
            'users_on_device': 48,
            'records_on_device': 1520,
          },
        });
      await t.pumpWidget(app(const DeviceDetailScreen(deviceId: 'd1'), _ov(repo, _admin, console: true)));
      await t.pumpAndSettle();
      expect(find.byKey(const ValueKey('edit-device')), findsOneWidget);
      await t.tap(find.byKey(const ValueKey('detail-test')));
      await t.pumpAndSettle();
      expect(find.byKey(const ValueKey('test-ok')), findsOneWidget);
      expect(find.text('SN-77'), findsOneWidget);
      expect(find.text('1520'), findsOneWidget);
    });
  });

  group('form', () {
    testWidgets('is not available outside the console or without device.manage', (t) async {
      final repo = FakeDeviceRepo();
      await t.pumpWidget(app(const DeviceFormScreen(), _ov(repo, _admin)));
      await t.pumpAndSettle();
      expect(find.text('You do not have permission to view this.'), findsOneWidget);
      await t.pumpWidget(app(const DeviceFormScreen(), _ov(repo, _hr, console: true)));
      await t.pumpAndSettle();
      expect(find.text('You do not have permission to view this.'), findsOneWidget);
    });

    testWidgets('validates and creates with a write-only key', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo();
      await t.pumpWidget(app(
        DeviceFormScreen(now: () => DateTime(2026, 11, 10, 9)),
        _ov(repo, _admin, console: true),
      ),);
      await t.pumpAndSettle();
      // The key field hides what is typed.
      final key = t.widget<TextField>(find.descendant(of: find.byKey(const ValueKey('f-key')), matching: find.byType(TextField)));
      expect(key.obscureText, isTrue);

      await t.tap(find.byKey(const ValueKey('form-save')));
      await t.pumpAndSettle();
      expect(find.text('Enter a name'), findsOneWidget);
      expect(find.text('Enter the IP address'), findsOneWidget);
      expect(repo.calls, isEmpty);

      await t.enterText(find.byKey(const ValueKey('f-name')), 'Gate 2');
      await t.enterText(find.byKey(const ValueKey('f-ip')), '192.168.0.202');
      await t.enterText(find.byKey(const ValueKey('f-key')), '1234');
      await t.tap(find.text('UDP'));
      await t.tap(find.byKey(const ValueKey('form-save')));
      await t.pumpAndSettle();
      expect(repo.calls, ['create']);
      expect(repo.lastBody!['comm_key'], 1234);
      expect(repo.lastBody!['transport'], 'udp');
      expect(repo.lastBody!['ingest_from'], '2026-11-10');
      expect(repo.lastBody!['port'], 4370);
    });

    testWidgets('edit never shows the saved key and keeps it unless asked', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo();
      final existing = repo.list.first;
      await t.pumpWidget(app(DeviceFormScreen(existing: existing, now: () => DateTime(2026, 11, 10)), _ov(repo, _admin, console: true)));
      await t.pumpAndSettle();
      expect(find.byKey(const ValueKey('f-key')), findsNothing);
      expect(find.text('Keep the saved key'), findsOneWidget);
      await t.tap(find.byKey(const ValueKey('form-save')));
      await t.pumpAndSettle();
      expect(repo.calls, ['update d1']);
      expect(repo.lastBody!.containsKey('comm_key'), isFalse);
    });

    testWidgets('edit can clear the key', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo();
      await t.pumpWidget(app(DeviceFormScreen(existing: repo.list.first, now: () => DateTime(2026, 11, 10)), _ov(repo, _admin, console: true)));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const ValueKey('key-clear')));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const ValueKey('form-save')));
      await t.pumpAndSettle();
      expect(repo.lastBody!.containsKey('comm_key'), isTrue);
      expect(repo.lastBody!['comm_key'], isNull);
    });

    testWidgets('setting a new key needs a valid number', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo();
      await t.pumpWidget(app(DeviceFormScreen(existing: repo.list.first, now: () => DateTime(2026, 11, 10)), _ov(repo, _admin, console: true)));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const ValueKey('key-set')));
      await t.pumpAndSettle();
      await t.enterText(find.byKey(const ValueKey('f-key')), 'abc');
      await t.tap(find.byKey(const ValueKey('form-save')));
      await t.pumpAndSettle();
      expect(find.text('The key is a number up to 999999'), findsOneWidget);
      expect(repo.calls, isEmpty);
    });

    testWidgets('server field and address errors are shown in words', (t) async {
      await _tall(t);
      final repo = _RejectingRepo();
      await t.pumpWidget(app(DeviceFormScreen(existing: repo.list.first, now: () => DateTime(2026, 11, 10)), _ov(repo, _admin, console: true)));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const ValueKey('form-save')));
      await t.pumpAndSettle();
      expect(find.textContaining('not allowed'), findsWidgets);
    });

    testWidgets('test connection before saving reports a plain error', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo()
        ..job = const DeviceSync(id: 'j1', kind: 'test', status: 'failed', errorCode: 'DEVICE_AUTH_FAILED');
      await t.pumpWidget(app(const DeviceFormScreen(), _ov(repo, _admin, console: true)));
      await t.pumpAndSettle();
      await t.enterText(find.byKey(const ValueKey('f-ip')), '192.168.0.203');
      await t.enterText(find.byKey(const ValueKey('f-key')), '55');
      await t.ensureVisible(find.byKey(const ValueKey('form-test')));
      await t.tap(find.byKey(const ValueKey('form-test')));
      await t.pumpAndSettle();
      expect(repo.calls, ['testSettings 192.168.0.203:4370 tcp key=55']);
      expect(find.byKey(const ValueKey('test-error')), findsOneWidget);
      expect(find.textContaining('communication key'), findsWidgets);
    });

    testWidgets('editing the address with a saved key asks for the key before testing', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo();
      await t.pumpWidget(app(DeviceFormScreen(existing: repo.list.first), _ov(repo, _admin, console: true)));
      await t.pumpAndSettle();
      await t.enterText(find.byKey(const ValueKey('f-ip')), '192.168.0.99');
      await t.ensureVisible(find.byKey(const ValueKey('form-test')));
      await t.tap(find.byKey(const ValueKey('form-test')));
      await t.pumpAndSettle();
      expect(repo.calls, isEmpty);
      expect(find.textContaining('enter the communication key again'), findsOneWidget);
    });
  });

  group('unmatched users', () {
    testWidgets('empty state', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo();
      await t.pumpWidget(app(const Scaffold(body: DeviceUsersTab()), _ov(repo, _hr)));
      await t.pumpAndSettle();
      expect(find.text('Every device user is linked to an employee.'), findsOneWidget);
    });

    testWidgets('view only cannot map', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo()..users = [_user('1')];
      await t.pumpWidget(app(const Scaffold(body: DeviceUsersTab()), _ov(repo, _view)));
      await t.pumpAndSettle();
      expect(find.textContaining('RAVI 1'), findsOneWidget);
      expect(find.textContaining('****4821'), findsOneWidget);
      expect(find.byKey(const ValueKey('map-1')), findsNothing);
      expect(find.byKey(const ValueKey('ignore-1')), findsNothing);
    });

    testWidgets('map to an employee on all devices', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo()..users = [_user('1')];
      const emp = Employee(id: 'e1', employeeId: 'E001', fullName: 'Ravi Kumar');
      await t.pumpWidget(app(Scaffold(body: DeviceUsersTab(pickEmployeeFn: (_) async => emp)), _ov(repo, _hr)));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const ValueKey('map-1')));
      await t.pumpAndSettle();
      expect(find.textContaining('Ravi Kumar'), findsOneWidget);
      await t.tap(find.byKey(const ValueKey('confirm-map')));
      await t.pumpAndSettle();
      expect(repo.calls, contains('map 1 e1 all=true'));
      expect(find.textContaining('Attendance is being updated'), findsOneWidget);
    });

    testWidgets('map on this device only when the box is cleared', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo()..users = [_user('1')];
      const emp = Employee(id: 'e1', employeeId: 'E001', fullName: 'Ravi Kumar');
      await t.pumpWidget(app(Scaffold(body: DeviceUsersTab(pickEmployeeFn: (_) async => emp)), _ov(repo, _hr)));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const ValueKey('map-1')));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const ValueKey('all-devices')));
      await t.tap(find.byKey(const ValueKey('confirm-map')));
      await t.pumpAndSettle();
      expect(repo.calls, contains('map 1 e1 all=false'));
    });

    testWidgets('ignore asks first and sends no employee', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo()..users = [_user('1')];
      await t.pumpWidget(app(const Scaffold(body: DeviceUsersTab()), _ov(repo, _hr)));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const ValueKey('ignore-1')));
      await t.pumpAndSettle();
      expect(repo.calls.where((c) => c.startsWith('map')), isEmpty);
      await t.tap(find.widgetWithText(FilledButton, 'Ignore'));
      await t.pumpAndSettle();
      expect(repo.calls, contains('map 1 ignore all=false'));
    });

    testWidgets('ignored users can be reset to automatic', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo()..users = [_user('2', status: 'ignored', method: 'ignored')];
      await t.pumpWidget(app(const Scaffold(body: DeviceUsersTab()), _ov(repo, _hr)));
      await t.pumpAndSettle();
      expect(find.byKey(const ValueKey('ignore-2')), findsNothing);
      await t.tap(find.byKey(const ValueKey('reset-2')));
      await t.pumpAndSettle();
      expect(repo.calls, contains('auto 2'));
    });

    testWidgets('filter chips reload the list', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo();
      await t.pumpWidget(app(const Scaffold(body: DeviceUsersTab()), _ov(repo, _hr)));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const ValueKey('users-matched')));
      await t.pumpAndSettle();
      expect(repo.calls, containsAll(['users unmatched', 'users matched']));
    });
  });

  group('punch log', () {
    testWidgets('shows outcomes including quarantined with the reason', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo()
        ..punchLog = [
          DevicePunch.fromJson({
            'id': '1',
            'device': {'id': 'd1', 'name': 'Main'},
            'device_user_id': '101',
            'user': {'id': 'e1', 'employee_id': 'E001', 'full_name': 'Ravi K'},
            'punch_at': '2026-11-10T03:32:11Z',
            'method': 'fingerprint',
            'outcome': 'check_in',
          }),
          DevicePunch.fromJson({
            'id': '2',
            'device': {'id': 'd1', 'name': 'Main'},
            'device_user_id': '102',
            'punch_at': '2026-11-10T03:40:11Z',
            'method': 'card',
            'outcome': 'quarantined',
            'reason': 'future',
          }),
        ];
      await t.pumpWidget(app(
        Scaffold(body: PunchLogTab(now: () => DateTime(2026, 11, 10, 12))),
        _ov(repo, _view),
      ),);
      await t.pumpAndSettle();
      expect(repo.calls.where((c) => c.startsWith('punches')).single, 'punches 2026-11-04..2026-11-10 any');
      expect(find.byKey(const ValueKey('outcome-2')), findsOneWidget);
      expect(find.text('Set aside'), findsOneWidget);
      expect(find.textContaining('time is in the future'), findsOneWidget);
      expect(find.textContaining('Ravi K (E001)'), findsOneWidget);
      expect(find.textContaining('Device user 102'), findsOneWidget);
      // Neutral wording: no technical or biometric method names.
      expect(find.textContaining('ingerprint'), findsNothing);
    });

    testWidgets('empty state', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo();
      await t.pumpWidget(app(const Scaffold(body: PunchLogTab()), _ov(repo, _view)));
      await t.pumpAndSettle();
      expect(find.text('No punches in this period.'), findsOneWidget);
    });

    testWidgets('outcome filter reloads with the chosen outcome', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo();
      await t.pumpWidget(app(
        Scaffold(body: PunchLogTab(now: () => DateTime(2026, 11, 10, 12))),
        _ov(repo, _view),
      ),);
      await t.pumpAndSettle();
      await t.tap(find.byKey(const ValueKey('punch-outcome')));
      await t.pumpAndSettle();
      await t.tap(find.text('Set aside').last);
      await t.pumpAndSettle();
      expect(repo.calls.last, 'punches 2026-11-04..2026-11-10 quarantined');
    });
  });

  group('employee view', () {
    testWidgets('My punches lists the day without device details', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo()
        ..mine = [
          MyPunch.fromJson({'punch_at': '2026-11-10T03:32:00Z', 'work_date': '2026-11-10', 'device_name': 'Main entrance', 'method': 'card', 'used_as': 'check_in'}),
          MyPunch.fromJson({'punch_at': '2026-11-10T03:33:00Z', 'work_date': '2026-11-10', 'device_name': 'Main entrance', 'method': 'card', 'used_as': 'none'}),
        ];
      await t.pumpWidget(app(
        Scaffold(body: MyPunchesTab(now: () => DateTime(2026, 11, 10, 12))),
        [deviceRepositoryProvider.overrideWithValue(repo)],
      ),);
      await t.pumpAndSettle();
      expect(repo.calls.single, 'mine 2026-11-10');
      expect(find.textContaining('Main entrance'), findsNWidgets(2));
      expect(find.textContaining('Counted as check-in'), findsOneWidget);
      expect(find.textContaining('Not counted'), findsOneWidget);
    });

    testWidgets('My punches empty and error states', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo();
      await t.pumpWidget(app(const Scaffold(body: MyPunchesTab()), [deviceRepositoryProvider.overrideWithValue(repo)]));
      await t.pumpAndSettle();
      expect(find.text('No office machine punches on this day.'), findsOneWidget);
      final bad = _FailingMine();
      await t.pumpWidget(app(const Scaffold(body: MyPunchesTab()), [deviceRepositoryProvider.overrideWithValue(bad)]));
      await t.pumpAndSettle();
      expect(find.text('Try again'), findsOneWidget);
    });

    testWidgets('record detail shows the source and device', (t) async {
      await _tall(t);
      final repo = FakeDeviceRepo()
        ..mine = [MyPunch.fromJson({'punch_at': '2026-11-10T03:32:00Z', 'device_name': 'Main entrance', 'method': 'card', 'used_as': 'check_in'})];
      final rec = AttendanceRecord.fromJson({
        'id': 'a',
        'work_date': '2026-11-10',
        'status': 'present',
        'check_in_at': '2026-11-10T03:32:00Z',
        'check_in_source': 'device',
        'check_in_device': {'id': 'd1', 'name': 'Main entrance'},
      });
      await t.pumpWidget(app(
        Builder(builder: (c) => TextButton(onPressed: () => showRecordDetail(c, rec), child: const Text('open'))),
        [deviceRepositoryProvider.overrideWithValue(repo)],
      ),);
      await t.tap(find.text('open'));
      await t.pumpAndSettle();
      expect(find.text('Check-in: Device (Main entrance)'), findsOneWidget);
      expect(find.byKey(const ValueKey('day-punches')), findsOneWidget);
    });

    testWidgets('privacy screen shows the notice', (t) async {
      await t.pumpWidget(app(const PrivacyScreen(), []));
      expect(find.byKey(const ValueKey('privacy-notice')), findsOneWidget);
      expect(find.textContaining('Fingerprint or face data stays on the machine'), findsOneWidget);
    });
  });

  group('manual attendance use device', () {
    testWidgets('HR can use device punches on a manual day; not for yourself', (t) async {
      await _tall(t);
      final admin = _FakeAdmin();
      Future<void> open(String self) async {
        await t.pumpWidget(app(
          ManualAttendanceScreen(now: () => DateTime(2026, 11, 10, 12)),
          [adminRepositoryProvider.overrideWithValue(admin), adminSelfIdProvider.overrideWithValue(self)],
        ),);
        await t.pumpAndSettle();
        await t.tap(find.text('Choose an employee'));
        await t.pumpAndSettle();
        await t.tap(find.text('Ravi Kumar'));
        await t.pumpAndSettle();
      }

      await open('someone-else');
      expect(find.byKey(const ValueKey('device-punches')), findsOneWidget);
      expect(find.byKey(const ValueKey('use-device')), findsOneWidget);

      await t.ensureVisible(find.byKey(const ValueKey('use-device')));
      await t.tap(find.byKey(const ValueKey('use-device')));
      await t.pumpAndSettle();
      // Reason is required (3 to 300 characters).
      await t.tap(find.byKey(const ValueKey('use-device-confirm')));
      await t.pumpAndSettle();
      expect(find.text('Give a reason (3 to 300 characters)'), findsOneWidget);
      await t.enterText(find.byKey(const ValueKey('use-device-reason')), 'Phone was fine');
      await t.tap(find.byKey(const ValueKey('use-device-confirm')));
      await t.pumpAndSettle();
      expect(admin.used, ['e1 2026-11-10 Phone was fine']);
      expect(find.byKey(const ValueKey('use-device')), findsNothing);

      admin.manual = true;
      await open('e1');
      expect(find.byKey(const ValueKey('use-device')), findsNothing);
    });
  });
}

class _RejectingRepo extends FakeDeviceRepo {
  @override
  Future<Device> updateDevice(String id, Map<String, Object?> body) async {
    throw const ApiException(code: 'DEVICE_ADDRESS_NOT_ALLOWED', message: 'x', status: 422);
  }
}

class _FailingMine extends FakeDeviceRepo {
  @override
  Future<List<MyPunch>> myPunches({String? date, String? from, String? to}) async {
    throw const ApiException(code: 'NETWORK_ERROR', message: 'No connection.');
  }
}

class _FakeAdmin extends AdminRepository {
  _FakeAdmin() : super(Dio());
  final List<String> used = [];
  bool manual = true;

  @override
  Future<Paged<Employee>> employees(EmployeeQuery q, {int page = 1}) async => const Paged(
        items: [Employee(id: 'e1', employeeId: 'E001', fullName: 'Ravi Kumar')],
        page: 1,
        total: 1,
        pageSize: 20,
      );

  @override
  Future<ManualAttendanceDay> manualAttendance(String userId, String workDate) async => ManualAttendanceDay.fromJson({
        'work_date': workDate,
        'user': {'id': userId, 'employee_id': 'E001', 'full_name': 'Ravi Kumar'},
        'record': {
          'id': 'a',
          'work_date': workDate,
          'status': 'present',
          'check_in_at': '2026-11-10T03:30:00Z',
          'manual': manual,
          'manual_reason': 'Forgot',
        },
        'device_punches': [
          {'punch_at': '2026-11-10T03:32:00Z', 'device_name': 'Main entrance', 'method': 'card', 'outcome': 'not_applied'},
        ],
      });

  @override
  Future<ManualAttendanceDay> useDevicePunches(String userId, String workDate, {required String reason}) async {
    used.add('$userId $workDate $reason');
    manual = false;
    return manualAttendance(userId, workDate);
  }
}
