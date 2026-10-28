import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/admin/domain/admin_models.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart';
import 'package:employee_connect/features/export/domain/export_models.dart';
import 'package:employee_connect/features/leave/data/leave_repository.dart';
import 'package:employee_connect/features/leave/domain/leave_logic.dart';
import 'package:employee_connect/features/leave/domain/leave_models.dart';
import 'package:flutter_test/flutter_test.dart';

/// On duty at client locations (plan 13).
void main() {
  const site = ClientLocationOption(id: 'c1', name: 'Acme Chennai', address: 'Mount Road');
  final today = DateTime(2026, 10, 21);

  group('models', () {
    test('on-duty request parses with its client location', () {
      final r = RequestItem.fromJson({
        'type': 'on_duty',
        'id': 'o1',
        'status': 'approved',
        'client_location': {'id': 'c1', 'name': 'Acme Chennai', 'address': 'Mount Road'},
        'from_date': '2026-10-21',
        'to_date': '2026-10-23',
        'days': 3,
        'reason': 'install',
      });
      expect(r.kind, RequestKind.onDuty);
      expect(r.title, 'On duty: Acme Chennai');
      expect(r.clientLocationAddress, 'Mount Road');
      expect(r.amountText, '3 days');
      expect(r.whenText, contains(' to '));
      expect(RequestKind.onDuty.wire, 'on_duty');
      expect(RequestKind.onDuty.path, 'on-duty');
      expect(RequestKind.permission.isDateRange, isFalse);
    });

    test('my requests merge all three kinds, newest first', () {
      final all = mergeRequests({
        'leave': {
          'items': [
            {'id': 'l', 'status': 'pending', 'created_at': '2026-10-01T00:00:00Z'},
          ],
        },
        'permission': {
          'items': [
            {'id': 'p', 'status': 'pending', 'created_at': '2026-10-02T00:00:00Z'},
          ],
        },
        'on_duty': {
          'items': [
            {'id': 'o', 'status': 'pending', 'created_at': '2026-10-03T00:00:00Z'},
          ],
        },
      });
      expect(all.map((e) => '${e.kind.wire}:${e.id}'), ['on_duty:o', 'permission:p', 'leave:l']);
    });

    test('today carries the on-duty site, record shape or null record', () {
      final empty = TodayAttendance.fromJson({
        'record': null,
        'on_duty': {'id': 'c1', 'name': 'Acme', 'lat': 13.08, 'lng': 80.27, 'radius_m': 200},
      });
      expect(empty.record, isNull);
      expect(empty.onDuty!.name, 'Acme');
      expect(empty.onDuty!.hasCoordinates, isTrue);
      expect(empty.onDuty!.radiusM, 200);

      final rec = TodayAttendance.fromJson({
        'id': 'a',
        'work_date': '2026-10-21',
        'status': 'present',
        'check_in_at': '2026-10-21T03:45:00Z',
        'client_location': {'id': 'c1', 'name': 'Acme'},
        'on_duty': null,
      });
      expect(rec.record!.clientLocationName, 'Acme');
      expect(rec.onDuty, isNull);
    });

    test('calendar day note names the client location', () {
      final c = CalendarMonth.fromJson({
        'month': '2026-10',
        'days': [
          {'date': '2026-10-21', 'status': 'present', 'on_duty': 'Acme'},
          {'date': '2026-10-22', 'status': 'holiday', 'holiday_name': 'Fest', 'on_duty': null},
        ],
      });
      expect(c.days.first.note, 'On duty: Acme');
      expect(c.days.last.note, 'Fest');
    });
  });

  group('validateOnDutyForm', () {
    String? v({ClientLocationOption? loc = site, DateTime? from, DateTime? to, String reason = 'install'}) =>
        validateOnDutyForm(location: loc, from: from ?? today, to: to ?? today, reason: reason, today: today);

    test('valid request', () => expect(v(), isNull));
    test('needs a location', () => expect(v(loc: null), 'Choose the client location'));
    test('no past dates', () => expect(v(from: DateTime(2026, 10, 20)), contains('past date')));
    test('to before from', () => expect(v(from: DateTime(2026, 10, 22)), contains('on or after')));
    test('31 days max', () {
      expect(v(to: DateTime(2026, 11, 20)), isNull);
      expect(v(to: DateTime(2026, 11, 21)), contains('31'));
    });
    test('needs a reason', () => expect(v(reason: '  '), 'Enter a reason'));
  });

  group('error text', () {
    test('server message wins for on-duty overlaps and dates', () {
      const e = ApiException(code: 'OVERLAPPING_REQUEST', message: 'You have a pending or approved leave on these dates');
      expect(onDutyErrorMessage(e), e.message);
    });

    test('inactive location asks to refresh', () {
      const e = ApiException(
        code: 'VALIDATION_ERROR',
        message: 'Choose an active client location',
        details: [FieldError('client_location_id', 'Choose an active client location')],
      );
      expect(onDutyErrorMessage(e), contains('no longer available'));
    });

    test('punch outside the client location names it', () {
      const e = ApiException(
        code: 'OUTSIDE_CLIENT_LOCATION',
        message: 'You are on duty today. Check in at the client location: Acme Chennai',
      );
      expect(attendanceErrorMessage(e), contains('Acme Chennai'));
    });
  });

  group('admin client locations', () {
    FieldSpec field(String key) => clientLocationSpec.fields.firstWhere((f) => f.key == key);

    test('coordinates accept 7 decimals and stay in range', () {
      expect(validateField(field('lat'), '13.0827123'), isNull);
      expect(validateField(field('lat'), '13.08271234'), 'At most 7 decimal places');
      expect(validateField(field('lat'), '91'), 'Must be at most 90');
      expect(validateField(field('lng'), '-181'), 'Must be at least -180');
      expect(validateField(field('radius_m'), '10'), 'Must be at least 20');
      expect(validateField(field('address'), ''), isNull);
    });

    test('0,0 is refused and the body is built for the API', () {
      final body = buildMasterBody(
        clientLocationSpec,
        {'name': 'Acme', 'address': '', 'lat': '13.0827', 'lng': '80.2707', 'radius_m': '200'},
        {'is_active': true},
      );
      expect(body, {'name': 'Acme', 'address': null, 'lat': 13.0827, 'lng': 80.2707, 'radius_m': 200, 'is_active': true});
      expect(validateMasterCross(clientLocationSpec, body), isNull);
      expect(validateMasterCross(clientLocationSpec, {...body, 'lat': 0.0, 'lng': 0.0}), contains('0, 0'));
    });

    test('section is shown only with client_location.manage', () {
      expect(adminSections(const ['client_location.manage']), [AdminSection.clientLocations]);
      expect(adminSections(const ['department.manage']), isNot(contains(AdminSection.clientLocations)));
    });

    test('leave type decimals stay at one place', () {
      final quota = leaveTypeSpec.fields.firstWhere((f) => f.key == 'annual_quota');
      expect(validateField(quota, '1.25'), 'At most one decimal place');
    });
  });

  group('export', () {
    test('On Duty export needs request view rights and is part of All', () {
      expect(canExportType(const ['export.team', 'request.view_team'], ExportType.onDuty), isTrue);
      expect(canExportType(const ['export.all', 'attendance.view_all'], ExportType.onDuty), isFalse);
      expect(exportTypeHasReasons(ExportType.onDuty), isTrue);
      expect(ExportType.onDuty.apiValue, 'on_duty');
    });
  });
}
