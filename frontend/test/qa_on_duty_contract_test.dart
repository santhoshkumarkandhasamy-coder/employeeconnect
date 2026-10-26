// QA: On Duty client logic checked against docs/plans/13-on-duty-contract.md (independent of the implementer's tests).
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/admin/domain/admin_models.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart';
import 'package:employee_connect/features/on_duty/domain/duty_admin_models.dart';
import 'package:employee_connect/features/on_duty/domain/on_duty_config.dart';
import 'package:employee_connect/features/on_duty/domain/on_duty_models.dart';
import 'package:employee_connect/features/on_duty/domain/on_duty_request_logic.dart';
import 'package:flutter_test/flutter_test.dart';

Map<String, dynamic> visitJson({Object? out, bool open = true, bool auto = false, Object? mins}) => {
      'id': 'v1',
      'work_date': '2026-10-26',
      'location': {'id': 'l1', 'name': 'Acme Client - Guindy'},
      'check_in_at': '2026-10-26T05:12:00Z',
      'check_out_at': out,
      'visit_minutes': mins,
      'open': open,
      'auto_closed': auto,
      'note': null,
      'check_in_selfie_url': null,
      'check_out_selfie_url': null,
      'request_id': null,
    };

void main() {
  group('/config on_duty block', () {
    test('missing, null or malformed block keeps the feature hidden', () {
      for (final v in [null, 'x', 5, <String, dynamic>{}]) {
        expect(OnDutyConfig.fromJson(v).showSwitch, isFalse, reason: '$v');
      }
    });
    test('switch iff enabled && has_locations', () {
      OnDutyConfig c(bool e, bool h) => OnDutyConfig.fromJson({'enabled': e, 'has_locations': h});
      expect(c(true, true).showSwitch, isTrue);
      expect(c(true, false).showSwitch, isFalse);
      expect(c(false, true).showSwitch, isFalse);
      expect(c(false, false).showSwitch, isFalse);
    });
    test('full contract sample parses', () {
      final c = OnDutyConfig.fromJson({
        'enabled': true,
        'has_locations': true,
        'approval_required': true,
        'selfie_required': true,
        'checkout_geofence': true,
        'max_visits_per_day': 3,
        'request_backdate_days': 2,
        'request_max_advance_days': 90,
      });
      expect(
        [c.approvalRequired, c.selfieRequired, c.checkoutGeofence, c.maxVisitsPerDay, c.requestBackdateDays, c.requestMaxAdvanceDays],
        [true, true, true, 3, 2, 90],
      );
    });
    test('strings are not truthy', () {
      expect(OnDutyConfig.fromJson({'enabled': 'true', 'has_locations': 1}).showSwitch, isFalse);
    });
  });

  group('DutyLocation (employee view)', () {
    test('parses fields', () {
      final l = DutyLocation.fromJson({'id': 'a', 'name': 'N', 'address': null, 'lat': 13.0067, 'lng': 80.2206, 'radius_m': 200});
      expect((l.id, l.name, l.address, l.lat, l.lng), ('a', 'N', null, 13.0067, 80.2206));
    });
    test('parseList tolerates junk', () {
      expect(DutyLocation.parseList(null), isEmpty);
      expect(DutyLocation.parseList({'items': 'x'}), isEmpty);
      expect(
        DutyLocation.parseList({
          'items': [
            1,
            null,
            {'id': 'a', 'name': 'N', 'lat': 1, 'lng': 2},
          ],
        }).length,
        1,
      );
    });
    test('integer coordinates parse as doubles', () {
      expect(DutyLocation.fromJson({'id': 'a', 'name': 'N', 'lat': 13, 'lng': 80}).lat, 13.0);
    });
  });

  group('Visit', () {
    test('open visit', () {
      final v = DutyVisit.fromJson(visitJson());
      expect(v.open, isTrue);
      expect(v.checkOutAt, isNull);
      expect(v.timeText.endsWith('- open'), isTrue);
      expect(v.locationName, 'Acme Client - Guindy');
    });
    test('closed visit shows duration', () {
      final v = DutyVisit.fromJson(visitJson(out: '2026-10-26T06:47:00Z', open: false, mins: 95));
      expect(v.open, isFalse);
      expect(v.visitMinutes, 95);
      expect(v.timeText, contains('1h 35m'));
    });
    test('auto-closed visit is not open and says no check-out recorded', () {
      final v = DutyVisit.fromJson(visitJson(open: false, auto: true));
      expect(v.open, isFalse);
      expect(v.autoClosed, isTrue);
      expect(v.timeText, contains('no check-out recorded'));
    });
    test('server open flag wins; derived only when absent', () {
      final j = visitJson()..remove('open');
      expect(DutyVisit.fromJson(j).open, isTrue);
      final k = visitJson(auto: true)..remove('open');
      expect(DutyVisit.fromJson(k).open, isFalse);
    });
  });

  group('/on-duty/today', () {
    test('contract sample', () {
      final t = OnDutyToday.fromJson({
        'work_date': '2026-10-26',
        'enabled': true,
        'attendance': null,
        'open_visit': visitJson(),
        'visits': [visitJson()],
        'visits_remaining': 8,
        'approval': {
          'required': false,
          'covered': true,
          'location_ids': ['l1'],
          'any_location': false,
        },
        'can_check_in': false,
        'blocked_reason': 'VISIT_OPEN',
      });
      expect(t.openVisit?.id, 'v1');
      expect(t.visits.length, 1);
      expect(t.visitsRemaining, 8);
      expect(t.canCheckIn, isFalse);
      expect(t.blockedReason, 'VISIT_OPEN');
      expect(t.approval.covered, isTrue);
      expect(t.attendance, isNull);
    });
    test('attendance record inside today is parsed with work_mode', () {
      final t = OnDutyToday.fromJson({
        'work_date': '2026-10-26',
        'attendance': {
          'id': 'r',
          'work_date': '2026-10-26',
          'status': 'present',
          'work_mode': 'mixed',
          'check_out_source': 'on_duty',
          'on_duty_locations': ['A', 'B'],
          'within_fence': null,
        },
      });
      expect(t.attendance!.workMode, 'mixed');
      expect(t.attendance!.hasOnDuty, isTrue);
      expect(t.attendance!.onDutyLocations, ['A', 'B']);
    });
    test('approval.blocks: required and not covered for the site', () {
      const a = OnDutyApproval(required: true, locationIds: ['l2']);
      expect(a.blocks('l1'), isTrue);
      expect(a.blocks('l2'), isFalse);
      expect(const OnDutyApproval(required: true, anyLocation: true).blocks('zzz'), isFalse);
      expect(const OnDutyApproval().blocks('l1'), isFalse);
    });
  });

  group('AttendanceRecord tolerance (contract 14.2)', () {
    test('within_fence null (day started on duty) is not shown as outside', () {
      final r = AttendanceRecord.fromJson({'id': 'r', 'work_date': '2026-10-26', 'status': 'present', 'within_fence': null, 'distance_m': null});
      expect(r.withinFence, isTrue);
      expect(r.distanceM, isNull);
    });
    test('unknown work_mode falls back to office; null check_out_source ok', () {
      final r = AttendanceRecord.fromJson({'id': 'r', 'work_date': '2026-10-26', 'status': 'present', 'work_mode': 'alien'});
      expect(r.workMode, 'office');
      expect(r.hasOnDuty, isFalse);
      expect(r.checkOutSource, isNull);
    });
    test('old server without the new fields still parses', () {
      final r = AttendanceRecord.fromJson({'id': 'r', 'work_date': '2026-10-26', 'status': 'late', 'minutes_late': 5, 'within_fence': true});
      expect(r.workMode, 'office');
      expect(r.onDutyLocations, isEmpty);
    });
    test('fromToday handles record:null and non-string locations', () {
      expect(AttendanceRecord.fromToday({'record': null}), isNull);
      final r = AttendanceRecord.fromJson({
        'id': 'r',
        'work_date': 'd',
        'status': 'present',
        'work_mode': 'on_duty',
        'on_duty_locations': ['A', 1, null, ''],
      });
      expect(r.onDutyLocations, ['A']);
    });
  });

  group('Admin models', () {
    test('DutyLocationAdmin', () {
      final l = DutyLocationAdmin.fromJson({
        'id': 'a',
        'name': 'N',
        'address': 'Road',
        'lat': 13,
        'lng': 80,
        'radius_m': 200,
        'assign_mode': 'all',
        'is_active': false,
        'assigned_count': 14,
        'in_use': true,
      });
      expect((l.radiusM, l.assignsAll, l.isActive, l.assignedCount, l.inUse), (200, true, false, 14, true));
      expect(l.assignText, contains('14'));
    });
    test('manager view of a visit has no admin detail', () {
      final v = AdminVisit.fromJson({
        ...visitJson(),
        'user': {'id': 'u', 'employee_id': 'E1', 'full_name': 'A B'},
        'check_in_distance_m': null,
        'check_out_distance_m': null,
        'check_out_within_fence': null,
        'check_in_accuracy_m': null,
        'check_out_accuracy_m': null,
        'suspicious': false,
      });
      expect(v.hasDetail, isFalse);
      expect(v.checkOutWithinFence, isNull);
      expect(v.suspicious, isFalse);
    });
    test('view_all visit flags outside check-out', () {
      final v = AdminVisit.fromJson({
        ...visitJson(),
        'user': {'id': 'u', 'employee_id': 'E1', 'full_name': 'A B'},
        'check_in_distance_m': 12,
        'check_out_distance_m': 340,
        'check_out_within_fence': false,
        'suspicious': true,
      });
      expect((v.checkInDistanceM, v.checkOutDistanceM, v.checkOutWithinFence, v.suspicious, v.hasDetail), (12, 340, false, true, true));
    });
    test('assignment body: user_ids always sent, departments only when chosen', () {
      expect(const AssignmentSelection().toBody(), {'assign_mode': 'selected', 'user_ids': <String>[]});
      expect(
        const AssignmentSelection(mode: 'all', departmentIds: ['d']).toBody(),
        {'assign_mode': 'all', 'user_ids': <String>[], 'add_department_ids': ['d']},
      );
    });
    test('admin sections gated by permission', () {
      expect(adminSections(const []).contains(AdminSection.dutyLocations), isFalse);
      expect(adminSections(const ['duty_location.manage']).contains(AdminSection.dutyLocations), isTrue);
      expect(adminSections(const ['employee.view']).contains(AdminSection.dutyLocations), isFalse);
      expect(adminSections(const ['attendance.view_team']).contains(AdminSection.onDutyVisits), isTrue);
      expect(adminSections(const ['attendance.view_team']).contains(AdminSection.dutyLocations), isFalse);
      expect(canOpenAdmin(const []), isFalse);
    });
  });

  group('Request validation (14.3)', () {
    final today = DateTime(2026, 10, 26);
    String? v(DateTime? f, DateTime? t, {String p = 'x', int back = 0, int adv = 60}) =>
        validateOnDutyRequest(from: f, to: t, purpose: p, today: today, backdateDays: back, maxAdvanceDays: adv);

    test('valid single and 31-day ranges', () {
      expect(v(today, today), isNull);
      expect(v(today, today.add(const Duration(days: 30))), isNull);
    });
    test('32 days is refused, 31 days accepted', () {
      expect(v(today, today.add(const Duration(days: 31))), isNotNull);
      expect(onDutyDays(today, today.add(const Duration(days: 30))), 31);
    });
    test('to before from', () => expect(v(today, today.subtract(const Duration(days: 1))), isNotNull));
    test('backdate window', () {
      expect(v(today.subtract(const Duration(days: 1)), today), isNotNull);
      expect(v(today.subtract(const Duration(days: 1)), today, back: 1), isNull);
      expect(v(today.subtract(const Duration(days: 2)), today, back: 1), isNotNull);
    });
    test('advance window', () {
      final far = today.add(const Duration(days: 60));
      expect(v(far, far), isNull);
      expect(v(far, far.add(const Duration(days: 1))), isNotNull);
    });
    test('purpose rules', () {
      expect(v(today, today, p: ''), isNotNull);
      expect(v(today, today, p: '   '), isNotNull);
      expect(v(today, today, p: 'x' * 300), isNull);
      expect(v(today, today, p: 'x' * 301), isNotNull);
    });
    test('missing dates', () {
      expect(v(null, today), isNotNull);
      expect(v(today, null), isNotNull);
    });
    test('day math across a month end', () {
      expect(onDutyDays(DateTime(2026, 2, 27), DateTime(2026, 3, 2)), 4);
    });
    test('body: null location = any, purpose trimmed, dates ymd', () {
      final b = onDutyRequestBody(from: DateTime(2026, 10, 28), to: DateTime(2026, 10, 30), locationId: null, purpose: '  Install  ');
      expect(b, {'from_date': '2026-10-28', 'to_date': '2026-10-30', 'location_id': null, 'purpose': 'Install'});
    });
  });

  group('Errors and notes', () {
    ApiException e(String c) => ApiException(code: c, message: 'SERVER-RAW', status: 409);
    test('mapped messages never echo the raw server text or a distance', () {
      for (final c in [
        'OUTSIDE_DUTY_LOCATION',
        'VISIT_OPEN',
        'NO_OPEN_VISIT',
        'MAX_VISITS_REACHED',
        'ON_DUTY_NOT_APPROVED',
        'DUTY_LOCATION_UNAVAILABLE',
        'ON_DUTY_DISABLED',
      ]) {
        final m = onDutyErrorMessage(e(c), locationName: 'Acme');
        expect(m, isNot(contains('SERVER-RAW')), reason: c);
        expect(RegExp(r'\d+ ?m\b').hasMatch(m), isFalse, reason: c);
      }
      expect(onDutyErrorMessage(e('OUTSIDE_DUTY_LOCATION'), locationName: 'Acme'), contains('You are outside Acme'));
    });
    test('reload set matches the contract mapping', () {
      for (final c in ['VISIT_OPEN', 'NO_OPEN_VISIT', 'DUTY_LOCATION_UNAVAILABLE', 'ON_DUTY_DISABLED', 'MAX_VISITS_REACHED', 'ON_DUTY_NOT_APPROVED']) {
        expect(onDutyNeedsReload(e(c)), isTrue, reason: c);
      }
      for (final c in ['OUTSIDE_DUTY_LOCATION', 'LOCATION_ACCURACY_LOW', 'RATE_LIMITED']) {
        expect(onDutyNeedsReload(e(c)), isFalse, reason: c);
      }
    });
    test('every blocked_reason has a text; unknown still gets a generic one; null is silent', () {
      for (final r in [
        'ON_DUTY_DISABLED',
        'NO_SHIFT_ASSIGNED',
        'NO_LOCATIONS',
        'ON_LEAVE',
        'VISIT_OPEN',
        'MAX_VISITS_REACHED',
        'ON_DUTY_NOT_APPROVED',
        'NEW_CODE',
      ]) {
        expect(blockedReasonText(r), isNotEmpty, reason: r);
      }
      expect(blockedReasonText(null), isNull);
    });
    test('note limit is 200 after trim, blank becomes null', () {
      expect(validateVisitNote('x' * 200), isNull);
      expect(validateVisitNote('x' * 201), isNotNull);
      expect(validateVisitNote('  ${'x' * 200}  '), isNull);
      expect(cleanNote('   '), isNull);
      expect(cleanNote(null), isNull);
      expect(cleanNote(' hi '), 'hi');
    });
  });

  group('Distance sorting and formatting', () {
    const a = DutyLocation(id: 'a', name: 'Alpha', lat: 13.0, lng: 80.0);
    const b = DutyLocation(id: 'b', name: 'beta', lat: 13.5, lng: 80.0);
    const c = DutyLocation(id: 'c', name: 'Charlie', lat: 13.0, lng: 80.0);
    test('nearest first, ties and no-fix by name (case-insensitive)', () {
      expect([for (final s in sortByDistance([b, a], lat: 13.0, lng: 80.0)) s.location.id], ['a', 'b']);
      expect([for (final s in sortByDistance([c, b, a])) s.location.id], ['a', 'b', 'c']);
      expect([for (final s in sortByDistance([c, a], lat: 13.0, lng: 80.0)) s.location.id], ['a', 'c']);
      expect(sortByDistance([]), isEmpty);
    });
    test('nearby hint', () {
      expect(sortByDistance([a], lat: 13.0, lng: 80.0).single.nearby, isTrue);
      expect(sortByDistance([b], lat: 13.0, lng: 80.0).single.nearby, isFalse);
      expect(sortByDistance([a]).single.nearby, isFalse);
    });
    test('formatDistance', () {
      expect(formatDistance(0), '10 m');
      expect(formatDistance(94), '90 m');
      expect(formatDistance(1000), '1.0 km');
      expect(formatDistance(14400), '14 km');
    });
  });

  group('Coordinate paste parser', () {
    test('plain pairs', () {
      expect(parseCoordinates('12.9716, 77.5946'), (lat: 12.9716, lng: 77.5946));
      expect(parseCoordinates('12.9716 77.5946'), (lat: 12.9716, lng: 77.5946));
      expect(parseCoordinates('-33.86;151.2'), (lat: -33.86, lng: 151.2));
    });
    test('map links', () {
      expect(parseCoordinates('https://www.google.com/maps/@13.0067,80.2206,17z'), (lat: 13.0067, lng: 80.2206));
      expect(parseCoordinates('https://maps.google.com/?q=13.0067,80.2206'), (lat: 13.0067, lng: 80.2206));
      expect(parseCoordinates('https://www.google.com/maps/place/X/data=!3d13.0067!4d80.2206'), (lat: 13.0067, lng: 80.2206));
    });
    test('rejects out of range, garbage, empty, short links, oversize input', () {
      for (final s in ['91, 10', '10, 181', '-91 0', 'abc', '', '   ', 'https://maps.app.goo.gl/abc123', '12.9716', 'NaN, NaN', '1e5, 1e5']) {
        expect(parseCoordinates(s), isNull, reason: s);
      }
      expect(parseCoordinates('1,1${'x' * 3000}'), isNull);
      expect(parseCoordinates(null), isNull);
    });
    test('boundary values accepted', () {
      expect(parseCoordinates('90, 180'), (lat: 90.0, lng: 180.0));
      expect(parseCoordinates('-90, -180'), (lat: -90.0, lng: -180.0));
    });
    test('maps link is built from the site only and rounded', () {
      expect(mapsLinkFor(13.00671234567, 80.22061234567), endsWith('query=13.006712,80.220612'));
    });
  });
}
