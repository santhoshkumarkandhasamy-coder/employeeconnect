import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/admin/domain/admin_models.dart';
import 'package:employee_connect/features/admin/domain/ops_models.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart';
import 'package:employee_connect/features/home/domain/home_models.dart';
import 'package:employee_connect/features/leave/data/leave_repository.dart';
import 'package:employee_connect/features/leave/domain/leave_logic.dart';
import 'package:employee_connect/features/leave/domain/leave_models.dart';
import 'package:employee_connect/features/on_duty/domain/duty_admin_models.dart';
import 'package:employee_connect/features/on_duty/domain/on_duty_config.dart';
import 'package:employee_connect/features/on_duty/domain/on_duty_models.dart';
import 'package:employee_connect/features/on_duty/domain/on_duty_request_logic.dart';
import 'package:flutter_test/flutter_test.dart';

ApiException err(String code, {int status = 409, List<FieldError> details = const []}) =>
    ApiException(code: code, message: 'server: $code', status: status, details: details);

void main() {
  group('config and models', () {
    test('/config.on_duty parses and the switch needs enabled AND has_locations', () {
      final c = AttendanceConfig.fromJson({
        'attendance': {'selfie_required': false},
        'on_duty': {
          'enabled': true,
          'has_locations': true,
          'approval_required': true,
          'selfie_required': true,
          'checkout_geofence': false,
          'max_visits_per_day': 4,
          'request_backdate_days': 2,
          'request_max_advance_days': 30,
        },
      });
      expect(c.onDuty.showSwitch, isTrue);
      expect(c.onDuty.approvalRequired, isTrue);
      expect(c.onDuty.maxVisitsPerDay, 4);
      expect(c.onDuty.requestMaxAdvanceDays, 30);
      expect(
        const OnDutyConfig(enabled: true).showSwitch,
        isFalse,
        reason: 'enabled but no assigned site',
      );
      expect(const OnDutyConfig(hasLocations: true).showSwitch, isFalse);
    });

    test('a server without the block keeps On Duty hidden', () {
      expect(AttendanceConfig.fromJson({'attendance': <String, dynamic>{}}).onDuty.showSwitch, isFalse);
      expect(OnDutyConfig.fromJson('nonsense').showSwitch, isFalse);
    });

    test('within_fence null (day started on duty) is not an outside signal', () {
      final r = AttendanceRecord.fromJson({
        'id': 'a',
        'work_date': '2026-10-26',
        'status': 'present',
        'check_in_at': '2026-10-26T05:12:00Z',
        'check_out_at': null,
        'within_fence': null,
        'distance_m': null,
        'work_mode': 'on_duty',
        'check_out_source': null,
        'on_duty_locations': ['Acme Client - Guindy'],
      });
      expect(r.withinFence, isTrue);
      expect(r.distanceM, isNull);
      expect(r.workMode, 'on_duty');
      expect(r.hasOnDuty, isTrue);
      expect(r.onDutyLocations, ['Acme Client - Guindy']);
      expect(r.checkedOut, isFalse);
    });

    test('a record without the new fields is an office day', () {
      final r = AttendanceRecord.fromJson({
        'id': 'a',
        'work_date': '2026-10-26',
        'status': 'present',
        'check_in_at': '2026-10-26T04:00:00Z',
        'check_out_at': '2026-10-26T12:00:00Z',
        'within_fence': true,
      });
      expect(r.workMode, 'office');
      expect(r.hasOnDuty, isFalse);
      expect(r.officeClosed, isTrue);
      expect(r.onDutyLocations, isEmpty);
    });

    test('a check-out set by a visit does not close the office day', () {
      final r = AttendanceRecord.fromJson({
        'id': 'a',
        'work_date': '2026-10-26',
        'status': 'present',
        'check_in_at': '2026-10-26T04:00:00Z',
        'check_out_at': '2026-10-26T10:00:00Z',
        'work_mode': 'mixed',
        'check_out_source': 'on_duty',
      });
      expect(r.checkedOut, isTrue);
      expect(r.officeClosed, isFalse);
      expect(r.workMode, 'mixed');
    });

    test('calendar day and summary carry the On Duty fields', () {
      final cal = CalendarMonth.fromJson({
        'month': '2026-10',
        'days': [
          {'date': '2026-10-26', 'status': 'present', 'on_duty': true},
          {'date': '2026-10-27', 'status': 'present'},
        ],
      });
      expect(cal.days[0].onDuty, isTrue);
      expect(cal.days[1].onDuty, isFalse);
      final s = AttendanceSummary.fromJson({'month': '2026-10', 'present': 5, 'on_duty_days': 3});
      expect(s.onDutyDays, 3);
      expect(AttendanceSummary.fromJson({'month': '2026-10'}).onDutyDays, 0);
    });

    test('today response: visits, open visit and approval', () {
      final t = OnDutyToday.fromJson({
        'work_date': '2026-10-26',
        'enabled': true,
        'attendance': null,
        'open_visit': {
          'id': 'v2',
          'work_date': '2026-10-26',
          'location': {'id': 'l1', 'name': 'Acme'},
          'check_in_at': '2026-10-26T05:12:00Z',
          'check_out_at': null,
          'open': true,
          'auto_closed': false,
        },
        'visits': [
          {
            'id': 'v1',
            'work_date': '2026-10-26',
            'location': {'id': 'l2', 'name': 'Branch'},
            'check_in_at': '2026-10-26T03:00:00Z',
            'check_out_at': '2026-10-26T04:35:00Z',
            'visit_minutes': 95,
            'open': false,
            'auto_closed': false,
            'note': 'Review',
          },
        ],
        'visits_remaining': 8,
        'approval': {'required': true, 'covered': true, 'location_ids': ['l1'], 'any_location': false},
        'can_check_in': false,
        'blocked_reason': 'VISIT_OPEN',
      });
      expect(t.openVisit?.locationName, 'Acme');
      expect(t.openVisit?.open, isTrue);
      expect(t.visits.single.visitMinutes, 95);
      expect(t.visits.single.note, 'Review');
      expect(t.visits.single.open, isFalse);
      expect(t.approval.blocks('l1'), isFalse);
      expect(t.approval.blocks('l2'), isTrue);
      expect(t.blockedReason, 'VISIT_OPEN');
      expect(blockedReasonText(t.blockedReason), contains('open visit'));
    });

    test('approval that is not required never blocks; any_location covers all', () {
      expect(const OnDutyApproval().blocks('x'), isFalse);
      expect(const OnDutyApproval(required: true, anyLocation: true).blocks('x'), isFalse);
      expect(const OnDutyApproval(required: true).blocks('x'), isTrue);
    });

    test('auto-closed visits are not open and say so', () {
      final v = DutyVisit.fromJson({
        'id': 'v',
        'location': {'id': 'l', 'name': 'Acme'},
        'check_in_at': '2026-10-25T05:00:00Z',
        'check_out_at': null,
        'auto_closed': true,
      });
      expect(v.open, isFalse);
      expect(v.timeText, contains('no check-out recorded'));
    });

    test('employee locations carry no radius', () {
      final l = DutyLocation.fromJson({
        'id': 'l',
        'name': 'Acme',
        'address': null,
        'lat': 13.0067,
        'lng': 80.2206,
        'radius_m': 200,
      });
      expect(l.lat, 13.0067);
      expect(l.address, isNull);
      // there is simply no radius field on the employee model
      expect(DutyLocation.parseList({'items': [<String, dynamic>{'id': 'a', 'name': 'B', 'lat': 1, 'lng': 2}]}).length, 1);
      expect(DutyLocation.parseList(null), isEmpty);
    });
  });

  group('home', () {
    test('/home today_attendance shows the open visit when there is one', () {
      final h = HomeData.fromJson({
        'today_attendance': {
          'check_in_at': '2026-10-26T04:00:00Z',
          'check_out_at': null,
          'status': 'present',
          'work_mode': 'mixed',
          'open_visit': {'id': 'v', 'location_name': 'Acme', 'check_in_at': '2026-10-26T05:12:00Z'},
        },
      });
      expect(h.attendance!.text, startsWith('On duty at Acme since '));
      final none = HomeData.fromJson({
        'today_attendance': {'check_in_at': '2026-10-26T04:00:00Z', 'status': 'present', 'open_visit': null},
      });
      expect(none.attendance!.text, startsWith('Checked in at'));
    });
  });

  group('error mapping', () {
    test('every new On Duty code has its own message', () {
      final codes = [
        'OUTSIDE_DUTY_LOCATION',
        'VISIT_OPEN',
        'NO_OPEN_VISIT',
        'MAX_VISITS_REACHED',
        'ON_DUTY_NOT_APPROVED',
        'DUTY_LOCATION_UNAVAILABLE',
        'ON_DUTY_DISABLED',
      ];
      final seen = <String>{};
      for (final c in codes) {
        final m = onDutyErrorMessage(err(c));
        expect(m, isNot('server: $c'), reason: c);
        expect(seen.add(m), isTrue, reason: 'distinct text for $c');
      }
    });

    test('OUTSIDE_DUTY_LOCATION names the site and never shows a distance', () {
      final m = onDutyErrorMessage(
        err('OUTSIDE_DUTY_LOCATION', status: 422, details: const [FieldError('within_fence', 'false')]),
        locationName: 'Acme Client - Guindy',
      );
      expect(m, contains('You are outside Acme Client - Guindy'));
      expect(m, isNot(contains(' m ')));
      expect(m, isNot(contains('false')));
    });

    test('ON_DUTY_NOT_APPROVED points to the request', () {
      expect(onDutyErrorMessage(err('ON_DUTY_NOT_APPROVED')), contains('Request On Duty'));
    });

    test('reload codes', () {
      for (final c in ['VISIT_OPEN', 'NO_OPEN_VISIT', 'DUTY_LOCATION_UNAVAILABLE', 'ON_DUTY_DISABLED']) {
        expect(onDutyNeedsReload(err(c)), isTrue, reason: c);
      }
      expect(onDutyNeedsReload(err('OUTSIDE_DUTY_LOCATION', status: 422)), isFalse);
    });

    test('shared codes fall back to the attendance wording', () {
      expect(onDutyErrorMessage(err('MOCK_LOCATION_DETECTED', status: 422)), contains('mock'));
      expect(onDutyErrorMessage(err('LOCATION_FIX_STALE', status: 422)), contains('out of date'));
      expect(onDutyErrorMessage(err('ON_LEAVE')), contains('leave'));
    });

    test('office End day: VISIT_OPEN and ALREADY_CHECKED_OUT wording', () {
      expect(attendanceErrorMessage(err('VISIT_OPEN')), contains('visit'));
      expect(attendanceErrorMessage(err('ALREADY_CHECKED_OUT')), contains('ended your day'));
    });

    test('request errors: clash date and unavailable site', () {
      expect(
        leaveErrorMessage(err('OVERLAPPING_REQUEST', details: const [FieldError('date', '2026-10-29')])),
        contains('2026-10-29'),
      );
      expect(leaveErrorMessage(err('DUTY_LOCATION_UNAVAILABLE', status: 422)), contains('not available'));
      expect(leaveErrorMessage(err('ON_DUTY_DISABLED')), contains('turned off'));
    });

    test('admin errors reuse IN_USE and CONFLICT', () {
      expect(adminErrorMessage(err('IN_USE')), contains('Deactivate'));
    });
  });

  group('site distances', () {
    const a = DutyLocation(id: 'a', name: 'Alpha', lat: 12.9716, lng: 77.5946);
    const b = DutyLocation(id: 'b', name: 'Bravo', lat: 12.9800, lng: 77.6000); // about 1 km away
    const c = DutyLocation(id: 'c', name: 'Charlie', lat: 12.97165, lng: 77.59465); // about 7 m away

    test('sorted nearest first with a nearby badge for close sites only', () {
      final s = sortByDistance([a, b, c], lat: 12.9716, lng: 77.5946);
      expect(s.map((e) => e.location.id), ['a', 'c', 'b']);
      expect(s[0].nearby, isTrue);
      expect(s[1].nearby, isTrue);
      expect(s[2].nearby, isFalse);
    });

    test('without a fix the order is by name and nothing is nearby', () {
      final s = sortByDistance([c, b, a]);
      expect(s.map((e) => e.location.name), ['Alpha', 'Bravo', 'Charlie']);
      expect(s.any((e) => e.nearby), isFalse);
      expect(s.first.meters, isNull);
    });

    test('distances are coarse', () {
      expect(formatDistance(3), '10 m');
      expect(formatDistance(87), '90 m');
      expect(formatDistance(1234), '1.2 km');
      expect(formatDistance(14600), '15 km');
    });

    test('maps link carries the site only', () {
      final u = mapsLinkFor(13.0067123456, 80.2206);
      expect(u, contains('13.006712,80.2206'));
    });
  });

  group('coordinate paste parser', () {
    test('plain pairs', () {
      expect(parseCoordinates('12.9716, 77.5946'), (lat: 12.9716, lng: 77.5946));
      expect(parseCoordinates('  12.9716 77.5946 '), (lat: 12.9716, lng: 77.5946));
      expect(parseCoordinates('12.9716;77.5946'), (lat: 12.9716, lng: 77.5946));
      expect(parseCoordinates('-33.86, 151.21'), (lat: -33.86, lng: 151.21));
      expect(parseCoordinates('13,80'), (lat: 13.0, lng: 80.0));
    });

    test('maps links', () {
      expect(
        parseCoordinates('https://www.google.com/maps/place/Acme/@13.0067,80.2206,17z/data=!3m1'),
        (lat: 13.0067, lng: 80.2206),
      );
      expect(
        parseCoordinates('https://maps.google.com/?q=13.0067,80.2206'),
        (lat: 13.0067, lng: 80.2206),
      );
      expect(
        parseCoordinates('https://www.google.com/maps/place/X/data=!3d13.0067!4d80.2206'),
        (lat: 13.0067, lng: 80.2206),
      );
      expect(
        parseCoordinates('https://maps.apple.com/?ll=13.0067%2C80.2206&z=10'),
        (lat: 13.0067, lng: 80.2206),
      );
    });

    test('rejects junk, out of range and short links', () {
      expect(parseCoordinates(''), isNull);
      expect(parseCoordinates(null), isNull);
      expect(parseCoordinates('hello'), isNull);
      expect(parseCoordinates('91, 10'), isNull);
      expect(parseCoordinates('10, 181'), isNull);
      expect(parseCoordinates('12.9'), isNull);
      expect(parseCoordinates('https://maps.app.goo.gl/AbCdEf'), isNull);
      expect(parseCoordinates('1,2,3'), isNull);
    });
  });

  group('request validation', () {
    final today = DateTime(2026, 10, 26);

    test('valid request', () {
      expect(
        validateOnDutyRequest(
          from: DateTime(2026, 10, 28),
          to: DateTime(2026, 10, 30),
          purpose: 'Installation',
          today: today,
        ),
        isNull,
      );
    });

    test('dates are required and ordered', () {
      expect(validateOnDutyRequest(from: null, to: null, purpose: 'x', today: today), isNotNull);
      expect(
        validateOnDutyRequest(from: DateTime(2026, 10, 30), to: DateTime(2026, 10, 28), purpose: 'x', today: today),
        contains('before'),
      );
    });

    test('span is at most 31 calendar days', () {
      expect(onDutyDays(DateTime(2026, 11, 1), DateTime(2026, 12, 1)), 31);
      expect(
        validateOnDutyRequest(from: DateTime(2026, 11, 1), to: DateTime(2026, 12, 1), purpose: 'x', today: today),
        isNull,
      );
      expect(
        validateOnDutyRequest(from: DateTime(2026, 11, 1), to: DateTime(2026, 12, 2), purpose: 'x', today: today),
        contains('31'),
      );
    });

    test('back-dating and advance limits come from settings', () {
      expect(
        validateOnDutyRequest(from: DateTime(2026, 10, 25), to: DateTime(2026, 10, 26), purpose: 'x', today: today),
        contains('past'),
      );
      expect(
        validateOnDutyRequest(
          from: DateTime(2026, 10, 24),
          to: DateTime(2026, 10, 26),
          purpose: 'x',
          today: today,
          backdateDays: 2,
        ),
        isNull,
      );
      expect(
        validateOnDutyRequest(
          from: DateTime(2026, 12, 20),
          to: DateTime(2026, 12, 30),
          purpose: 'x',
          today: today,
          maxAdvanceDays: 60,
        ),
        contains('60'),
      );
    });

    test('purpose is 1-300 characters', () {
      expect(validatePurpose('  '), isNotNull);
      expect(validatePurpose(null), isNotNull);
      expect(validatePurpose('a'), isNull);
      expect(validatePurpose('a' * 300), isNull);
      expect(validatePurpose('a' * 301), isNotNull);
    });

    test('body uses null for any location and trims the purpose', () {
      final b = onDutyRequestBody(
        from: DateTime(2026, 10, 28),
        to: DateTime(2026, 10, 30),
        locationId: null,
        purpose: ' Install ',
      );
      expect(b, {
        'from_date': '2026-10-28',
        'to_date': '2026-10-30',
        'location_id': null,
        'purpose': 'Install',
      });
    });

    test('notes are limited to 200 and blank becomes null', () {
      expect(validateVisitNote('a' * 200), isNull);
      expect(validateVisitNote('a' * 201), isNotNull);
      expect(cleanNote('   '), isNull);
      expect(cleanNote(' hi '), 'hi');
    });
  });

  group('requests in the existing lists', () {
    test('an on-duty request parses into a RequestItem', () {
      final r = RequestItem.fromJson({
        'id': 'r1',
        'from_date': '2026-10-28',
        'to_date': '2026-10-30',
        'days': 3,
        'location': {'id': 'l', 'name': 'Acme'},
        'purpose': 'Installation',
        'status': 'pending',
        'created_at': '2026-10-26T05:00:00Z',
      }, kind: RequestKind.onDuty,);
      expect(r.isOnDuty, isTrue);
      expect(r.title, 'On duty');
      expect(r.amountText, '3 days');
      expect(r.reason, 'Installation');
      expect(r.locationText, 'Acme');
      expect(r.whenText, contains('to'));
    });

    test('null location means any assigned location; admin items detect the type', () {
      final r = RequestItem.fromJson({
        'id': 'r1',
        'type': 'on_duty',
        'from_date': '2026-10-28',
        'to_date': '2026-10-28',
        'days': 1,
        'location': null,
        'purpose': 'x',
        'status': 'approved',
        'user': {'id': 'u', 'employee_id': 'E1', 'full_name': 'Asha'},
      });
      expect(r.kind, RequestKind.onDuty);
      expect(r.locationText, 'Any assigned location');
      expect(r.amountText, '1 day');
      expect(r.user?.display, 'Asha');
    });

    test('request kinds map to URL segments', () {
      expect(RequestKind.onDuty.wire, 'on_duty');
      expect(RequestKind.onDuty.path, 'on-duty');
      expect(RequestKind.leave.path, 'leave');
    });

    test('leave items flag an overlapping approved on-duty request', () {
      final r = RequestItem.fromJson({
        'id': 'l1',
        'type': 'leave',
        'from_date': '2026-10-28',
        'to_date': '2026-10-28',
        'days': 1,
        'status': 'pending',
        'on_duty_conflict': true,
      });
      expect(r.onDutyConflict, isTrue);
      expect(RequestItem.fromJson({'id': 'x', 'type': 'leave', 'status': 'pending'}).onDutyConflict, isFalse);
    });

    test('mergeRequests includes on-duty items and sorts newest first', () {
      Map<String, dynamic> page(String id, String created, [String extra = '']) => {
            'items': [
              {'id': id, 'status': 'pending', 'created_at': created, 'days': 1},
            ],
          };
      final all = mergeRequests({
        'leave': page('a', '2026-10-01T00:00:00Z'),
        'permission': page('b', '2026-10-02T00:00:00Z'),
        'on_duty': page('c', '2026-10-03T00:00:00Z'),
      });
      expect(all.map((e) => e.id), ['c', 'b', 'a']);
      expect(all.first.kind, RequestKind.onDuty);
      // a server without On Duty: the key is simply empty
      expect(mergeRequests({'leave': page('a', '2026-10-01T00:00:00Z'), 'permission': <String, dynamic>{}, 'on_duty': <String, dynamic>{}}).length, 1);
    });

    test('push: on_duty_decision opens My Requests', () {
      final t = parseRequestPush({'type': 'on_duty_decision', 'id': 'r1'});
      expect(t?.screen, RequestPushScreen.myRequests);
      expect(t?.kind, RequestKind.onDuty);
      expect(t?.id, 'r1');
      expect(parseRequestPush({'type': 'new_request', 'request_type': 'on_duty', 'id': 'r1'})?.screen,
          RequestPushScreen.approvals,);
    });
  });

  group('admin: forms and permissions', () {
    test('field validators', () {
      expect(validateDutyName('A'), isNotNull);
      expect(validateDutyName(' Ab '), isNull);
      expect(validateDutyName('a' * 81), isNotNull);
      expect(validateLatitude('90'), isNull);
      expect(validateLatitude('90.1'), isNotNull);
      expect(validateLatitude('abc'), isNotNull);
      expect(validateLongitude('-180'), isNull);
      expect(validateLongitude('181'), isNotNull);
      expect(validateDutyRadius('9'), isNotNull);
      expect(validateDutyRadius('10'), isNull);
      expect(validateDutyRadius('2000'), isNull);
      expect(validateDutyRadius('2001'), isNotNull);
      expect(validateDutyRadius('1.5'), isNotNull);
      expect(validateDutyAddress('a' * 301), isNotNull);
    });

    test('location body', () {
      final b = dutyLocationBody(
        name: ' Acme ',
        address: ' ',
        lat: '13.00671234',
        lng: '80.2206',
        radius: '200',
        assignMode: 'all',
        isActive: false,
      );
      expect(b['name'], 'Acme');
      expect(b['address'], isNull);
      expect(b['lat'], 13.006712);
      expect(b['radius_m'], 200);
      expect(b['assign_mode'], 'all');
      expect(b['is_active'], false);
    });

    test('admin location parses counts and flags', () {
      final l = DutyLocationAdmin.fromJson({
        'id': 'l',
        'name': 'Acme',
        'lat': 13.0,
        'lng': 80.0,
        'radius_m': 200,
        'assign_mode': 'all',
        'is_active': true,
        'assigned_count': 14,
        'in_use': true,
      });
      expect(l.assignsAll, isTrue);
      expect(l.assignText, 'All employees (14)');
      expect(l.inUse, isTrue);
      expect(DutyLocationAdmin.fromJson({'id': 'x', 'name': 'y'}).assignMode, 'selected');
    });

    test('sections: duty_location.manage and attendance view permissions', () {
      expect(adminSections(['duty_location.manage']), [AdminSection.dutyLocations]);
      expect(adminSections(['attendance.view_all']), [AdminSection.onDutyVisits]);
      expect(adminSections(['attendance.view_team']), [AdminSection.onDutyVisits]);
      expect(adminSections(['attendance.view_own']), isEmpty);
      expect(canManageDutyLocations(['x']), isFalse);
      expect(canSeeVisitDetail(['attendance.view_team']), isFalse);
      expect(canSeeVisitDetail(['attendance.view_all']), isTrue);
      expect(canViewDutyVisits(['attendance.view_team']), isTrue);
    });

    test('settings: the eight on_duty keys with the contract ranges', () {
      final specs = {for (final s in settingSpecs) s.key: s};
      for (final k in [
        'on_duty.enabled',
        'on_duty.approval_required',
        'on_duty.apply_late_rule',
        'on_duty.checkout_geofence',
        'on_duty.selfie_required',
        'on_duty.max_visits_per_day',
        'on_duty.request_backdate_days',
        'on_duty.request_max_advance_days',
      ]) {
        expect(specs.containsKey(k), isTrue, reason: k);
        expect(specs[k]!.group, 'On Duty');
      }
      expect(specs['on_duty.max_visits_per_day']!.min, 1);
      expect(specs['on_duty.max_visits_per_day']!.max, 50);
      expect(specs['on_duty.request_backdate_days']!.max, 30);
      expect(specs['on_duty.request_max_advance_days']!.min, 1);
      expect(specs['on_duty.request_max_advance_days']!.max, 365);
      expect(specs['on_duty.enabled']!.kind, SettingKind.boolean);
      expect(validateSetting(specs['on_duty.max_visits_per_day']!, '51'), isNotNull);
      expect(validateSetting(specs['on_duty.max_visits_per_day']!, '10'), isNull);
    });

    test('admin visit: managers get null detail fields', () {
      final v = AdminVisit.fromJson({
        'id': 'v',
        'work_date': '2026-10-26',
        'location': {'id': 'l', 'name': 'Acme'},
        'check_in_at': '2026-10-26T05:00:00Z',
        'check_out_at': null,
        'open': true,
        'user': {'id': 'u', 'employee_id': 'E1', 'full_name': 'Asha'},
        'check_in_distance_m': null,
        'check_out_distance_m': null,
        'check_out_within_fence': null,
        'suspicious': false,
      });
      expect(v.hasDetail, isFalse);
      expect(v.checkOutWithinFence, isNull);
      final full = AdminVisit.fromJson({
        'id': 'v',
        'location': {'id': 'l', 'name': 'Acme'},
        'check_in_distance_m': 12,
        'check_out_distance_m': 340,
        'check_out_within_fence': false,
        'suspicious': true,
      });
      expect(full.hasDetail, isTrue);
      expect(full.checkOutWithinFence, isFalse);
      expect(full.suspicious, isTrue);
    });

    test('visit query only sends what is set', () {
      expect(const VisitQuery().toQuery(), isEmpty);
      expect(
        const VisitQuery(date: '2026-10-26', locationId: 'l', employeeId: ' E1 ', openOnly: true).toQuery(),
        {'date': '2026-10-26', 'location_id': 'l', 'employee_id': 'E1', 'open': true},
      );
    });

    test('console day rows: mode label and null for no record', () {
      final r = AttendanceDayRow.fromJson({
        'user': {'id': 'u', 'employee_id': 'E1', 'full_name': 'Asha'},
        'status': 'present',
        'work_mode': 'mixed',
        'on_duty_locations': ['Acme', 'Branch'],
      });
      expect(r.modeLabel, 'Office + On Duty');
      expect(r.onDutyLocations, ['Acme', 'Branch']);
      final none = AttendanceDayRow.fromJson({'status': 'not_checked_in', 'work_mode': null});
      expect(none.workMode, isNull);
      expect(none.modeLabel, isEmpty);
    });

    test('dashboard: on-duty pending joins the total', () {
      final d = Dashboard.fromJson({
        'requests': {'leave_pending': 2, 'permission_pending': 1, 'on_duty_pending': 4},
        'attendance': {'on_duty': 12, 'on_duty_open': 5},
      });
      expect(d.pendingRequests, 7);
      expect(d.attendance!['on_duty'], 12);
      expect(Dashboard.fromJson({'requests': {'leave_pending': 2, 'permission_pending': 1}}).pendingRequests, 3);
    });
  });

  group('assignment selection', () {
    const u1 = EmployeeRef(id: '1', employeeId: 'E1', fullName: 'Asha Rao');
    const u2 = EmployeeRef(id: '2', employeeId: 'E2', fullName: 'Bala K');
    const u3 = EmployeeRef(id: '3', employeeId: 'E3', fullName: 'Chitra');

    test('toggle adds and removes; no duplicates', () {
      var s = const AssignmentSelection();
      s = s.toggle(u1).toggle(u2);
      expect(s.count, 2);
      expect(s.contains('1'), isTrue);
      s = s.toggle(u1);
      expect(s.contains('1'), isFalse);
      expect(s.count, 1);
      s = s.addAll([u2, u3, u3]);
      expect(s.users.map((u) => u.id), ['2', '3']);
    });

    test('body always carries user_ids; departments only when chosen', () {
      var s = const AssignmentSelection(mode: 'all').toggle(u1);
      expect(s.toBody(), {'assign_mode': 'all', 'user_ids': ['1']});
      s = s.toggleDepartment('d1').toggleDepartment('d2').toggleDepartment('d1');
      expect(s.toBody(), {
        'assign_mode': 'all',
        'user_ids': ['1'],
        'add_department_ids': ['d2'],
      });
      expect(const AssignmentSelection().toBody()['user_ids'], isEmpty);
    });

    test('mode switch keeps the list', () {
      final s = const AssignmentSelection().toggle(u1).toggle(u2).withMode('all');
      expect(s.isAll, isTrue);
      expect(s.count, 2);
      expect(s.withMode('selected').isAll, isFalse);
      expect(s.withMode('weird').isAll, isFalse);
    });

    test('clear, remove and local filter', () {
      var s = const AssignmentSelection().toggle(u1).toggle(u2).toggle(u3);
      expect(s.filtered('bal').map((u) => u.id), ['2']);
      expect(s.filtered('E3').map((u) => u.id), ['3']);
      expect(s.filtered('  ').length, 3);
      s = s.remove('2');
      expect(s.count, 2);
      expect(s.clear().count, 0);
    });

    test('limits', () {
      final many = [
        for (var i = 0; i < 5001; i++) EmployeeRef(id: '$i', employeeId: 'E$i', fullName: 'N$i'),
      ];
      final s = const AssignmentSelection().addAll(many);
      expect(s.overLimit, isTrue);
      expect(s.problem, contains('5000'));
      expect(const AssignmentSelection().problem, isNull);
    });

    test('assignments response parses users', () {
      final a = DutyAssignments.fromJson({
        'location_id': 'l',
        'assign_mode': 'all',
        'items': [
          {
            'user': {'id': '1', 'employee_id': 'E1', 'full_name': 'Asha'},
            'assigned_at': '2026-10-01T00:00:00Z',
          },
          {'user': null},
        ],
      });
      expect(a.assignMode, 'all');
      expect(a.users.single.id, '1');
    });
  });
}
