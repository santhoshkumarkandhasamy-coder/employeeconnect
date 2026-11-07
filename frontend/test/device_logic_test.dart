import 'package:employee_connect/console/console_shell.dart' show visibleDestinations;
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/admin/domain/admin_models.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart';
import 'package:employee_connect/features/devices/domain/device_models.dart';
import 'package:employee_connect/features/devices/presentation/my_punches.dart';
import 'package:employee_connect/features/attendance/presentation/widgets/today_status_card.dart';
import 'package:flutter_test/flutter_test.dart';

/// Attendance devices (plan 18, api-contract.md section 21): models, validation, messages, gating.
Map<String, dynamic> deviceJson({String status = 'ok', Map<String, dynamic>? extra}) => {
      'id': 'd1',
      'name': 'Main entrance',
      'location_label': 'Ground floor',
      'ip': '192.168.0.201',
      'port': 4370,
      'transport': 'tcp',
      'has_comm_key': true,
      'enabled': true,
      'timezone': 'Asia/Kolkata',
      'clock_correction': 'auto',
      'ingest_from': '2026-11-10',
      'serial_number': 'ABC123',
      'model': 'K40',
      'firmware': 'Ver 6.60',
      'status': status,
      'clock_status': 'ok',
      'last_clock_offset_s': -14,
      'last_sync_at': '2026-11-10T09:00:00Z',
      'last_success_at': '2026-11-10T09:00:00Z',
      'last_error_code': null,
      'records_on_device': 1520,
      'users_on_device': 48,
      'unmatched_users': 3,
      ...?extra,
    };

void main() {
  group('device model', () {
    test('parses the API shape and never carries a key', () {
      final d = Device.fromJson(deviceJson(extra: {
        'recent_syncs': [
          {'id': 's1', 'kind': 'manual', 'status': 'ok', 'fetched': 10, 'inserted': 2, 'quarantined': 1},
        ],
      },),);
      expect(d.name, 'Main entrance');
      expect(d.address, '192.168.0.201:4370');
      expect(d.hasCommKey, isTrue);
      expect(d.unmatchedUsers, 3);
      expect(d.recentSyncs.single.summary, 'Synced: 10 records read, 2 new, 1 set aside.');
      expect(deviceJson().containsKey('comm_key'), isFalse);
    });

    test('status labels cover failing, offline, disabled, drift', () {
      expect(deviceStatusLabel(Device.fromJson(deviceJson())), 'OK');
      expect(deviceStatusLabel(Device.fromJson(deviceJson(status: 'failing'))), 'Failing');
      expect(deviceStatusLabel(Device.fromJson(deviceJson(status: 'offline'))), 'Offline (paused)');
      expect(deviceStatusLabel(Device.fromJson(deviceJson(status: 'disabled'))), 'Disabled');
      expect(deviceStatusLabel(Device.fromJson(deviceJson(status: 'never_synced'))), 'Never synced');
      expect(deviceStatusLabel(Device.fromJson(deviceJson(extra: {'clock_status': 'drift'}))), 'Clock drift');
      expect(deviceStatusLabel(Device.fromJson(deviceJson(extra: {'clock_status': 'invalid'}))), 'Clock wrong');
    });

    test('relative time and clock offset text', () {
      final now = DateTime(2026, 11, 10, 12);
      expect(relativeTime(null), 'never');
      expect(relativeTime(now.subtract(const Duration(seconds: 20)), now: now), 'just now');
      expect(relativeTime(now.subtract(const Duration(minutes: 5)), now: now), '5 min ago');
      expect(relativeTime(now.subtract(const Duration(hours: 3)), now: now), '3 h ago');
      expect(relativeTime(now.subtract(const Duration(days: 2)), now: now), '2 d ago');
      expect(clockOffsetText(-14), '-14 s');
      expect(clockOffsetText(600), '+10 min');
      expect(clockOffsetText(7200), '+2 h');
      expect(clockOffsetText(null), '-');
    });

    test('a test job result exposes serial, model and counts', () {
      final s = DeviceSync.fromJson({
        'id': 't1',
        'kind': 'test',
        'status': 'ok',
        'result': {
          'serial_number': 'SN1',
          'model': 'K40',
          'firmware': 'v1',
          'device_time': '2026-11-10T09:02:11',
          'clock_offset_s': 4,
          'users_on_device': 5,
          'records_on_device': 90,
        },
      });
      expect(s.isFinished, isTrue);
      expect(s.summary, 'Connection works.');
      expect(s.result!.serialNumber, 'SN1');
      expect(s.result!.recordsOnDevice, 90);
      expect(DeviceSync.fromJson({'id': 'x', 'kind': 'manual', 'status': 'running'}).isFinished, isFalse);
    });

    test('device user, punch and my punch parse', () {
      final u = DeviceUser.fromJson({
        'id': 'u1',
        'device': {'id': 'd1', 'name': 'Main'},
        'device_user_id': '101',
        'name': 'RAVI K',
        'card_masked': '****4821',
        'status': 'matched',
        'match_method': 'manual',
        'user': {'id': 'e1', 'employee_id': 'E001', 'full_name': 'Ravi K'},
        'unapplied_punches': 4,
      });
      expect(u.isMatched, isTrue);
      expect(u.user!.display, 'Ravi K (E001)');
      expect(u.cardMasked, '****4821');
      final p = DevicePunch.fromJson({
        'id': '123',
        'device': {'id': 'd1', 'name': 'Main'},
        'device_user_id': '101',
        'punch_at': '2026-11-10T03:32:11Z',
        'outcome': 'quarantined',
        'reason': 'clock_invalid',
        'method': 'fingerprint',
      });
      expect(p.id, '123');
      expect(punchOutcomeLabel(p.outcome), 'Set aside');
      expect(punchReasonLabel(p.reason), 'machine clock was wrong');
      final m = MyPunch.fromJson({'punch_at': '2026-11-10T03:32:11Z', 'device_name': 'Main', 'method': 'card', 'used_as': 'check_in'});
      expect(m.usedAsLabel, 'Counted as check-in');
      expect(MyPunch.fromJson(const {}).usedAsLabel, 'Not counted');
    });

    test('method labels never use biometric wording', () {
      for (final m in ['fingerprint', 'face', 'palm', 'card', 'password', 'other', '']) {
        expect(punchMethodLabel(m).toLowerCase(), isNot(anyOf(contains('finger'), contains('face'), contains('palm'), contains('bio'))));
      }
    });
  });

  group('permissions', () {
    test('view, map, manage and sync gating', () {
      expect(canViewDevices(const ['device.view']), isTrue);
      expect(canViewDevices(const ['device.map']), isFalse);
      expect(canMapDevices(const ['device.map']), isTrue);
      expect(canManageDevices(const ['device.manage']), isTrue);
      expect(canSyncDevices(const ['device.view']), isFalse);
      expect(canSyncDevices(const ['device.map']), isTrue);
      expect(canSyncDevices(const ['device.manage']), isTrue);
    });

    test('console menu shows Attendance devices only with device.view', () {
      expect(visibleDestinations(const ['device.view']).map((d) => d.label), contains('Attendance devices'));
      expect(visibleDestinations(const ['device.manage', 'device.map']).map((d) => d.label), isNot(contains('Attendance devices')));
    });

    test('admin home lists Attendance devices only with device.view', () {
      expect(adminSections(const ['device.view']), [AdminSection.devices]);
      expect(adminSections(const ['device.manage']), isNot(contains(AdminSection.devices)));
    });
  });

  group('form validation', () {
    test('name, ip, port', () {
      expect(validateDeviceName(''), isNotNull);
      expect(validateDeviceName('a' * 61), isNotNull);
      expect(validateDeviceName('Main'), isNull);
      expect(validateDeviceIp('192.168.0.201'), isNull);
      expect(validateDeviceIp('192.168.0'), isNotNull);
      expect(validateDeviceIp('192.168.0.300'), isNotNull);
      expect(validateDeviceIp('host.local'), isNotNull);
      expect(validateDevicePort('4370'), isNull);
      expect(validateDevicePort('0'), isNotNull);
      expect(validateDevicePort('70000'), isNotNull);
      expect(validateDevicePort('x'), isNotNull);
    });

    test('communication key is up to six digits', () {
      expect(validateCommKey('0'), isNull);
      expect(validateCommKey('999999'), isNull);
      expect(validateCommKey('1000000'), isNotNull);
      expect(validateCommKey('12a'), isNotNull);
      expect(validateCommKey(''), isNotNull);
    });

    test('timezone and ingest-from window', () {
      expect(validateDeviceTimezone(''), isNull);
      expect(validateDeviceTimezone('Asia/Kolkata'), isNull);
      expect(validateDeviceTimezone('UTC'), isNull);
      expect(validateDeviceTimezone('banana'), isNotNull);
      final now = DateTime(2026, 11, 10, 15);
      expect(validateIngestFrom(DateTime(2026, 11, 10), now), isNull);
      expect(validateIngestFrom(DateTime(2026, 10, 10), now), isNull);
      expect(validateIngestFrom(DateTime(2024, 3, 25), now), isNull); // history is allowed back to the retention
      expect(validateIngestFrom(DateTime(2023, 1, 1), now), isNotNull);
      expect(validateIngestFrom(DateTime(2026, 11, 11), now), isNotNull);
      expect(validateIngestFrom(null, now), isNotNull);
    });

    test('punch range is at most 92 days', () {
      expect(validatePunchRange(DateTime(2026, 11, 1), DateTime(2026, 11, 10)), isNull);
      expect(validatePunchRange(DateTime(2026, 11, 10), DateTime(2026, 11, 1)), isNotNull);
      expect(validatePunchRange(DateTime(2026, 1, 1), DateTime(2026, 4, 2)), isNull); // 92 days inclusive
      expect(validatePunchRange(DateTime(2026, 1, 1), DateTime(2026, 4, 3)), isNotNull);
    });

    Map<String, Object?> body(CommKeyAction a, {bool create = false, String key = ''}) => deviceBody(
          name: ' Main ',
          ip: '192.168.0.201',
          port: '4370',
          transport: 'udp',
          keyAction: a,
          commKey: key,
          enabled: true,
          location: ' ',
          timezone: '',
          ingestFrom: DateTime(2026, 11, 10),
          create: create,
        );

    test('comm key: keep omits it, clear sends null, set sends the number', () {
      final keep = body(CommKeyAction.keep);
      expect(keep.containsKey('comm_key'), isFalse);
      expect(body(CommKeyAction.clear)['comm_key'], isNull);
      expect(body(CommKeyAction.clear).containsKey('comm_key'), isTrue);
      expect(body(CommKeyAction.set, key: '1234')['comm_key'], 1234);
      expect(body(CommKeyAction.keep, create: true).containsKey('comm_key'), isTrue);
      expect(keep['name'], 'Main');
      expect(keep['location_label'], isNull);
      expect(keep['timezone'], isNull);
      expect(keep['transport'], 'udp');
      expect(keep['ingest_from'], '2026-11-10');
      expect(keep['port'], 4370);
    });
  });

  group('error messages', () {
    test('every device code has plain words and no technical jargon', () {
      const codes = [
        'DEVICE_UNREACHABLE',
        'DEVICE_TIMEOUT',
        'DEVICE_AUTH_FAILED',
        'DEVICE_BUSY',
        'DEVICE_PROTOCOL_ERROR',
        'DEVICE_IDENTITY_CHANGED',
        'DEVICE_CLOCK_INVALID',
        'CIRCUIT_OPEN',
        'DEVICE_ADDRESS_NOT_ALLOWED',
        'DEVICE_DISABLED',
        'TEMPORARILY_UNAVAILABLE',
        'INVALID_DATE_RANGE',
      ];
      for (final c in codes) {
        final t = deviceErrorText(c);
        expect(t, isNotNull, reason: c);
        expect(t, isNot(contains('DEVICE_')), reason: c);
        expect(t!.toLowerCase(), isNot(anyOf(contains('biometric'), contains('fingerprint'), contains('face'))), reason: c);
      }
      expect(deviceErrorText('SOMETHING_ELSE'), isNull);
    });

    test('job text prefers the known code, then the server words, then a generic line', () {
      expect(deviceJobErrorText('DEVICE_BUSY'), contains('Another program'));
      expect(deviceJobErrorText('ODD', 'Custom words'), 'Custom words');
      expect(deviceJobErrorText(null), contains('did not finish'));
    });

    test('admin error mapping uses them for HTTP errors too', () {
      expect(
        adminErrorMessage(const ApiException(code: 'DEVICE_ADDRESS_NOT_ALLOWED', message: 'x', status: 422)),
        contains('not allowed'),
      );
      expect(adminErrorMessage(const ApiException(code: 'DEVICE_DISABLED', message: 'x', status: 409)), contains('switched off'));
      expect(adminErrorMessage(const ApiException(code: 'TEMPORARILY_UNAVAILABLE', message: 'x', status: 503)), contains('sync service'));
      expect(adminErrorMessage(const ApiException(code: 'DEVICE_IDENTITY_CHANGED', message: 'x')), contains('different machine'));
      expect(adminErrorMessage(const ApiException(code: 'IN_USE', message: 'x', status: 409)), contains('in use'));
    });
  });

  group('settings', () {
    test('device keys are in the settings form with their ranges', () {
      SettingSpec spec(String k) => settingSpecs.firstWhere((s) => s.key == k);
      const keys = [
        'device.sync_interval_minutes',
        'device.sync_window_start',
        'device.sync_window_end',
        'device.off_window_interval_minutes',
        'device.apply_window_days',
        'device.checkout_min_gap_minutes',
        'device.drift_tolerance_s',
        'device.drift_invalid_s',
        'device.future_tolerance_s',
        'device.match_prefix',
        'device.apply_on_on_duty_days',
        'device.use_punch_state',
        'device.push_on_punch',
        'device.punch_retention_days',
        'device.sync_history_retention_days',
      ];
      for (final k in keys) {
        expect(spec(k).group, 'Attendance devices', reason: k);
      }
      expect(validateSetting(spec('device.sync_interval_minutes'), '1'), isNotNull);
      expect(validateSetting(spec('device.sync_interval_minutes'), '5'), isNull);
      expect(validateSetting(spec('device.sync_window_start'), '25:00'), isNotNull);
      expect(validateSetting(spec('device.sync_window_start'), '05:00'), isNull);
      expect(validateSetting(spec('device.drift_invalid_s'), '100'), isNotNull);
    });

    test('match prefix and punch retention special rules', () {
      final prefix = settingSpecs.firstWhere((s) => s.key == 'device.match_prefix');
      expect(validateSetting(prefix, ''), isNull);
      expect(validateSetting(prefix, 'EMP-'), isNull);
      expect(validateSetting(prefix, 'bad prefix!'), isNotNull);
      expect(validateSetting(prefix, 'ABCDEFGHIJK'), isNotNull);
      expect(parseSettingValue(prefix, ' EMP '), 'EMP');
      final keep = settingSpecs.firstWhere((s) => s.key == 'device.punch_retention_days');
      expect(validateSetting(keep, '0'), isNull);
      expect(validateSetting(keep, '89'), isNotNull);
      expect(validateSetting(keep, '90'), isNull);
    });
  });

  group('attendance sources', () {
    test('record parses source and device', () {
      final r = AttendanceRecord.fromJson({
        'id': 'a',
        'work_date': '2026-11-10',
        'status': 'present',
        'check_in_at': '2026-11-10T03:32:00Z',
        'check_in_source': 'device',
        'check_out_source': 'app',
        'check_in_device': {'id': 'd1', 'name': 'Main entrance'},
        'check_out_device': null,
      });
      expect(r.checkInSource, 'device');
      expect(r.checkOutSource, 'app');
      expect(r.checkInDeviceName, 'Main entrance');
      expect(r.checkOutDeviceName, isNull);
    });

    test('old payloads default to the phone', () {
      final r = AttendanceRecord.fromJson({'id': 'a', 'work_date': '2026-11-10', 'status': 'present'});
      expect(r.checkInSource, 'app');
      expect(r.checkOutSource, isNull);
    });

    test('today card names the office device', () {
      final r = AttendanceRecord.fromJson({
        'id': 'a',
        'work_date': '${DateTime.now().year.toString().padLeft(4, '0')}-'
            '${DateTime.now().month.toString().padLeft(2, '0')}-${DateTime.now().day.toString().padLeft(2, '0')}',
        'status': 'present',
        'check_in_at': DateTime.now().toUtc().toIso8601String(),
        'check_in_source': 'device',
        'check_in_device': {'id': 'd1', 'name': 'Main entrance'},
      });
      expect(todayStatusText(r), contains('at Office device (Main entrance)'));
    });

    test('source lines and labels', () {
      expect(sourceLabel('app'), 'Phone');
      expect(sourceLabel('device'), 'Device');
      expect(sourceLabel('manual'), 'HR');
      expect(sourceLabel(null), '');
      final r = AttendanceRecord.fromJson({
        'id': 'a',
        'work_date': '2026-11-10',
        'status': 'present',
        'check_in_at': '2026-11-10T03:32:00Z',
        'check_out_at': '2026-11-10T12:32:00Z',
        'check_in_source': 'device',
        'check_out_source': 'manual',
        'check_in_device': {'id': 'd1', 'name': 'Main'},
      });
      expect(sourceLines(r), ['Check-in: Device (Main)', 'Check-out: HR']);
    });

    test('manual attendance day lists device punches', () {
      final d = ManualAttendanceDay.fromJson({
        'work_date': '2026-11-10',
        'record': null,
        'device_punches': [
          {'punch_at': '2026-11-10T03:32:00Z', 'device_name': 'Main', 'method': 'card', 'outcome': 'check_in'},
        ],
      });
      expect(d.devicePunches.single.deviceName, 'Main');
      expect(ManualAttendanceDay.fromJson({'work_date': 'x'}).devicePunches, isEmpty);
    });
  });

  test('privacy notice is the plan text and the only biometric wording', () {
    expect(devicePrivacyNotice, startsWith('Office attendance machines: Employee Connect receives only your device user number'));
    expect(devicePrivacyNotice, contains('never copied to Employee Connect'));
  });
}
