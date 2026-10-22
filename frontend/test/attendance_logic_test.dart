import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/cache/json_cache.dart';
import 'package:employee_connect/core/utils/dates.dart';
import 'package:employee_connect/core/utils/geo.dart';
import 'package:employee_connect/features/attendance/data/attendance_repository.dart';
import 'package:employee_connect/features/attendance/data/location_service.dart';
import 'package:employee_connect/features/attendance/data/selfie_service.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart';
import 'package:employee_connect/features/attendance/presentation/attendance_providers.dart';
import 'package:employee_connect/features/late/domain/late_models.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/attendance_fakes.dart';

const _fence = ApiException(
  code: 'OUTSIDE_GEOFENCE',
  message: 'Outside',
  status: 422,
  details: [FieldError('within_fence', 'false')],
);

ProviderContainer _container(FakeAttendanceRepo repo, FakeLocation loc, FakeSelfie selfie) {
  final c = ProviderContainer(overrides: [
    attendanceRepositoryProvider.overrideWithValue(repo),
    locationServiceProvider.overrideWithValue(loc),
    selfieServiceProvider.overrideWithValue(selfie),
  ],);
  addTearDown(c.dispose);
  // keep the auto-dispose controller alive for the test
  c.listen(checkControllerProvider, (_, __) {});
  return c;
}

void main() {
  group('dates', () {
    test('month helpers', () {
      expect(shiftMonth('2026-12', 1), '2027-01');
      expect(shiftMonth('2026-01', -1), '2025-12');
      expect(monthTitle('2026-10'), 'October 2026');
      expect(monthRange('2026-02'), (from: '2026-02-01', to: '2026-02-28'));
      expect(parseMonth('2026-13'), isNull);
      expect(parseYmd('2026-02-30'), isNull);
    });
    test('formatting', () {
      expect(formatDuration(450), '7h 30m');
      expect(formatDuration(45), '45m');
      expect(formatDuration(120), '2h');
      expect(formatDayLong(DateTime(2026, 10, 21)), 'Wed, 21 Oct 2026');
      expect(formatTime(DateTime(2026, 1, 1, 9, 5)), '09:05');
      expect(formatAverageMinutes(21.66), '21.7 min');
      expect(formatAverageMinutes(20), '20 min');
    });
  });

  test('haversine is roughly right', () {
    expect(haversineMeters(12.97, 77.59, 12.97, 77.59), 0);
    // 0.001 deg latitude is about 111 m
    expect(haversineMeters(12.970, 77.59, 12.971, 77.59), closeTo(111, 2));
  });

  group('json cache', () {
    test('expires by ttl', () async {
      var now = DateTime(2026, 1, 1);
      final c = MemoryJsonCache(now: () => now);
      await c.write('k', {'a': 1});
      expect(await c.read('k', maxAge: const Duration(hours: 1)), {'a': 1});
      now = now.add(const Duration(hours: 2));
      expect(await c.read('k', maxAge: const Duration(hours: 1)), isNull);
      expect(await c.read('k'), isNotNull);
    });

    test('cache never stores distance, fence, selfie url or coordinates', () async {
      final cache = MemoryJsonCache();
      await fetchWithCache<int>(
        cache: cache,
        key: 'h',
        fetch: () async => {
          'n': 1,
          'items': [
            {'id': 'a', 'distance_m': 12, 'within_fence': true, 'selfie_url': '/files/x', 'lat': 1.0},
          ],
        },
        parse: (j) => 1,
      );
      expect(await cache.read('h'), {
        'n': 1,
        'items': [
          {'id': 'a'},
        ],
      });
    });

    test('fetchWithCache falls back only on network errors', () async {
      final cache = MemoryJsonCache();
      int parse(Object j) => (j as Map<String, dynamic>)['n'] as int;
      final fresh = await fetchWithCache<int>(
        cache: cache,
        key: 'x',
        fetch: () async => {'n': 5},
        parse: parse,
      );
      expect(fresh.value, 5);
      expect(fresh.stale, isFalse);

      final offline = await fetchWithCache<int>(
        cache: cache,
        key: 'x',
        fetch: () async => throw const ApiException(
          code: ApiException.networkCode,
          message: 'offline',
        ),
        parse: parse,
      );
      expect(offline.value, 5);
      expect(offline.stale, isTrue);

      expect(
        () => fetchWithCache<int>(
          cache: cache,
          key: 'x',
          fetch: () async => throw const ApiException(code: 'FORBIDDEN', message: 'no', status: 403),
          parse: parse,
        ),
        throwsA(isA<ApiException>()),
      );
      expect(
        () => fetchWithCache<int>(
          cache: cache,
          key: 'missing',
          fetch: () async => throw const ApiException(code: ApiException.networkCode, message: 'offline'),
          parse: parse,
        ),
        throwsA(isA<ApiException>()),
      );
    });
  });

  group('models', () {
    test('today: null record and full record', () {
      expect(AttendanceRecord.fromToday({'record': null}), isNull);
      final r = AttendanceRecord.fromToday({
        'id': 'a',
        'work_date': '2026-10-21',
        'check_in_at': '2026-10-21T03:45:00Z',
        'status': 'late',
        'minutes_late': 22,
        'shift': {'start': '09:30', 'end': '18:30', 'grace_minutes': 10},
        'auto_closed': false,
      })!;
      expect(r.status, DayStatus.late);
      expect(r.minutesLate, 22);
      expect(r.checkedIn, isTrue);
      expect(r.checkedOut, isFalse);
      expect(r.shiftStart, '09:30');
    });

    test('calendar and summary parse; unknown status is tolerated', () {
      final c = CalendarMonth.fromJson({
        'month': '2026-10',
        'days': [
          {'date': '2026-10-01', 'status': 'holiday', 'holiday_name': 'Gandhi Jayanti'},
          {'date': '2026-10-02', 'status': 'weird'},
          {'date': 'bad', 'status': 'present'},
        ],
      });
      expect(c.days.length, 2);
      expect(c.days.first.note, 'Gandhi Jayanti');
      expect(c.days.last.status, DayStatus.unknown);
      final s = AttendanceSummary.fromJson({'month': '2026-10', 'present': 3, 'half_day': 1});
      expect(s.present, 3);
      expect(s.halfDay, 1);
      expect(s.absent, 0);
    });

    test('config reads selfie and office', () {
      final c = AttendanceConfig.fromJson({
        'attendance': {'selfie_required': true},
        'office': {'lat': 12.97, 'lng': 77.59, 'radius_m': 150},
      });
      expect(c.selfieRequired, isTrue);
      expect(c.hasOffice, isTrue);
      expect(c.radiusM, 150);
    });

    test('paging flag', () {
      final p = Paged.parse<int>(
        {'items': [1, 2].map((e) => {'v': e}).toList(), 'page': 1, 'page_size': 2, 'total': 5},
        (m) => m['v'] as int,
      );
      expect(p.hasMore, isTrue);
    });

    test('late models', () {
      final r = LateRecord.fromJson({
        'id': 'l',
        'work_date': '2026-10-05',
        'minutes_late': 22,
        'shift_start': '09:30',
        'grace_minutes': 10,
        'check_in_at': '2026-10-05T04:22:00Z',
      });
      expect(r.minutesLate, 22);
      final s = LateSummary.fromJson({
        'month': '2026-10',
        'late_count': 3,
        'total_minutes_late': 65,
        'average_minutes_late': 21.7,
      });
      expect(s.lateCount, 3);
      expect(s.averageMinutesLate, 21.7);
    });

    test('geofence error without details falls back to a generic message', () {
      const e = ApiException(code: 'OUTSIDE_GEOFENCE', message: 'Outside', status: 422);
      final m = attendanceErrorMessage(e);
      expect(m, contains('outside the office area'));
      expect(m, isNot(contains(' m ')));
      expect(AttendanceRecord.fromJson({'id': 'a', 'status': 'present'}).distanceM, isNull);
    });

    test('error messages', () {
      final m = attendanceErrorMessage(_fence);
      expect(m, contains('outside the office area'));
      expect(m, isNot(contains(RegExp(r'[0-9]+ m'))));
      String msg(String code, [Duration? retry]) =>
          attendanceErrorMessage(ApiException(code: code, message: 'x', retryAfter: retry));
      expect(msg('ON_LEAVE'), contains('leave'));
      expect(msg('LOCATION_FIX_STALE'), contains('Refresh your location'));
      expect(msg('MOCK_LOCATION_DETECTED'), contains('mock'));
      expect(msg('SELFIE_STALE'), contains('Retake'));
      expect(msg('SELFIE_REUSED'), contains('new selfie'));
      expect(msg('RATE_LIMITED', const Duration(seconds: 120)), contains('2 minutes'));
      expect(msg('RATE_LIMITED', const Duration(seconds: 45)), contains('45 seconds'));
      for (final c in ['DATA_UNREADABLE', 'FILE_UNAVAILABLE', 'TEMPORARILY_UNAVAILABLE']) {
        expect(msg(c), contains('temporarily unavailable'));
      }
    });

    test('previous-day open record shows the work date and allows check-out', () {
      final r = AttendanceRecord.fromJson({
        'id': 'n',
        'work_date': '2000-01-01',
        'status': 'present',
        'check_in_at': '2000-01-01T16:30:00Z',
        'check_out_at': null,
      });
      expect(r.checkedIn && !r.checkedOut, isTrue);
    });
  });

  group('CheckController', () {
    test('check-in success without selfie when optional', () async {
      final repo = FakeAttendanceRepo();
      final c = _container(repo, FakeLocation(), FakeSelfie());
      final ok = await c
          .read(checkControllerProvider.notifier)
          .submit(checkOut: false, confirmRationale: () async => true);
      expect(ok, isTrue);
      expect(repo.checkInSelfieIds, [null]);
      expect(c.read(checkControllerProvider).success, startsWith('Checked in at'));
    });

    test('asks for rationale before the permission prompt; declining stops', () async {
      final loc = FakeLocation(state: LocationAccess.denied);
      final c = _container(FakeAttendanceRepo(), loc, FakeSelfie());
      final ok = await c
          .read(checkControllerProvider.notifier)
          .submit(checkOut: false, confirmRationale: () async => false);
      expect(ok, isFalse);
      expect(loc.requests, 0);
      expect(c.read(checkControllerProvider).error, contains('permission'));
    });

    test('granting after rationale proceeds', () async {
      final loc = FakeLocation(state: LocationAccess.denied);
      final c = _container(FakeAttendanceRepo(), loc, FakeSelfie());
      final ok = await c
          .read(checkControllerProvider.notifier)
          .submit(checkOut: false, confirmRationale: () async => true);
      expect(ok, isTrue);
      expect(loc.requests, 1);
    });

    test('denied forever and service off block with settings hint', () async {
      for (final a in [LocationAccess.deniedForever, LocationAccess.serviceOff]) {
        final c = _container(FakeAttendanceRepo(), FakeLocation(state: a), FakeSelfie());
        final ok = await c
            .read(checkControllerProvider.notifier)
            .submit(checkOut: false, confirmRationale: () async => true);
        expect(ok, isFalse);
        expect(c.read(checkControllerProvider).blockedAccess, a);
      }
    });

    test('required selfie is captured, uploaded and sent', () async {
      final repo = FakeAttendanceRepo()
        ..cfg = const AttendanceConfig(selfieRequired: true);
      final selfie = FakeSelfie('/tmp/s.jpg');
      final c = _container(repo, FakeLocation(), selfie);
      final ok = await c
          .read(checkControllerProvider.notifier)
          .submit(checkOut: false, confirmRationale: () async => true);
      expect(ok, isTrue);
      expect(selfie.captures, 1);
      expect(repo.checkInSelfieIds, ['file-1']);
    });

    test('required selfie cancelled blocks check-in', () async {
      final repo = FakeAttendanceRepo()
        ..cfg = const AttendanceConfig(selfieRequired: true);
      final c = _container(repo, FakeLocation(), FakeSelfie());
      final ok = await c
          .read(checkControllerProvider.notifier)
          .submit(checkOut: false, confirmRationale: () async => true);
      expect(ok, isFalse);
      expect(repo.checkInSelfieIds, isEmpty);
      expect(c.read(checkControllerProvider).selfieNeeded, isTrue);
    });

    test('optional selfie upload failure still checks in', () async {
      final repo = FakeAttendanceRepo()
        ..uploadError = const ApiException(code: 'FILE_TOO_LARGE', message: 'big', status: 422);
      final selfie = FakeSelfie('/tmp/s.jpg');
      final c = _container(repo, FakeLocation(), selfie);
      final notifier = c.read(checkControllerProvider.notifier);
      await notifier.pickSelfie();
      final ok = await notifier.submit(checkOut: false, confirmRationale: () async => true);
      expect(ok, isTrue);
      expect(repo.checkInSelfieIds, [null]);
    });

    test('geofence error shows a message without distances', () async {
      final repo = FakeAttendanceRepo()..checkInError = _fence;
      final c = _container(repo, FakeLocation(), FakeSelfie());
      final ok = await c
          .read(checkControllerProvider.notifier)
          .submit(checkOut: false, confirmRationale: () async => true);
      expect(ok, isFalse);
      expect(c.read(checkControllerProvider).error, contains('outside the office area'));
    });

    test('SELFIE_STALE / SELFIE_REUSED force a fresh selfie, never reusing the file id', () async {
      for (final code in ['SELFIE_STALE', 'SELFIE_REUSED']) {
        final repo = FakeAttendanceRepo()
          ..cfg = const AttendanceConfig(selfieRequired: true)
          ..checkInError = ApiException(code: code, message: 'x', status: 422);
        final selfie = FakeSelfie('/tmp/s.jpg');
        final c = _container(repo, FakeLocation(), selfie);
        final n = c.read(checkControllerProvider.notifier);
        expect(await n.submit(checkOut: false, confirmRationale: () async => true), isFalse);
        final s = c.read(checkControllerProvider);
        expect(s.selfieNeeded, isTrue);
        expect(s.selfiePath, isNull);
        expect(s.error, contains('selfie'));
        // Retry captures and uploads again.
        repo.checkInError = null;
        expect(await n.submit(checkOut: false, confirmRationale: () async => true), isTrue);
        expect(selfie.captures, 2);
        expect(repo.checkInSelfieIds, ['file-1', 'file-1']);
        expect(repo.uploads, 2);
      }
    });

    test('LOCATION_FIX_STALE, MOCK_LOCATION_DETECTED and 429 surface friendly errors', () async {
      for (final (code, text) in [
        ('LOCATION_FIX_STALE', 'Refresh your location'),
        ('MOCK_LOCATION_DETECTED', 'mock'),
        ('RATE_LIMITED', 'wait'),
        ('TEMPORARILY_UNAVAILABLE', 'temporarily unavailable'),
      ]) {
        final repo = FakeAttendanceRepo()
          ..checkInError = ApiException(
              code: code, message: 'x', status: 422, retryAfter: const Duration(seconds: 60),);
        final c = _container(repo, FakeLocation(), FakeSelfie());
        final ok = await c
            .read(checkControllerProvider.notifier)
            .submit(checkOut: false, confirmRationale: () async => true);
        expect(ok, isFalse);
        expect(c.read(checkControllerProvider).error, contains(text));
      }
    });

    test('mock location flag is surfaced', () async {
      final loc = FakeLocation(
        fix: LocationFix(lat: 1, lng: 1, accuracyM: 5, isMock: true, timestamp: DateTime.now()),
      );
      final c = _container(FakeAttendanceRepo(), loc, FakeSelfie());
      await c
          .read(checkControllerProvider.notifier)
          .submit(checkOut: false, confirmRationale: () async => true);
      expect(c.read(checkControllerProvider).mockLocation, isTrue);
    });

    test('missing or poor accuracy is refused and nothing is submitted', () async {
      final bad = [
        LocationFix(lat: 1, lng: 1, accuracyM: 0, timestamp: DateTime.now()),
        LocationFix(lat: 1, lng: 1, accuracyM: double.nan, timestamp: DateTime.now()),
        LocationFix(lat: 1, lng: 1, accuracyM: 500, timestamp: DateTime.now()),
        const LocationFix(lat: 1, lng: 1, accuracyM: 10),
      ];
      for (final f in bad) {
        final repo = FakeAttendanceRepo();
        final c = _container(repo, FakeLocation(fix: f), FakeSelfie());
        final ok = await c
            .read(checkControllerProvider.notifier)
            .submit(checkOut: false, confirmRationale: () async => true);
        expect(ok, isFalse);
        expect(repo.punches, isEmpty);
        expect(c.read(checkControllerProvider).error, isNotNull);
      }
    });

    test('accuracy and fix timestamp reach the repository', () async {
      final repo = FakeAttendanceRepo();
      final c = _container(repo, FakeLocation(), FakeSelfie());
      await c
          .read(checkControllerProvider.notifier)
          .submit(checkOut: false, confirmRationale: () async => true);
      expect(repo.punches.single.accuracyM, 10);
      expect(repo.punches.single.timestamp, isNotNull);
    });

    test('approximate-only location shows the precise-location message with settings', () async {
      final loc = FakeLocation(state: LocationAccess.reducedAccuracy);
      final c = _container(FakeAttendanceRepo(), loc, FakeSelfie());
      final ok = await c
          .read(checkControllerProvider.notifier)
          .submit(checkOut: false, confirmRationale: () async => true);
      expect(ok, isFalse);
      final s = c.read(checkControllerProvider);
      expect(s.error, preciseLocationMessage);
      expect(s.blockedAccess, LocationAccess.reducedAccuracy);
    });

    test('selfie file is deleted after upload, success or failure', () async {
      final ok = FakeSelfie('/tmp/a.jpg');
      final c1 = _container(
          FakeAttendanceRepo()..cfg = const AttendanceConfig(selfieRequired: true),
          FakeLocation(),
          ok,);
      await c1
          .read(checkControllerProvider.notifier)
          .submit(checkOut: false, confirmRationale: () async => true);
      expect(ok.discarded, ['/tmp/a.jpg']);

      final fail = FakeSelfie('/tmp/b.jpg');
      final c2 = _container(
          FakeAttendanceRepo()
            ..cfg = const AttendanceConfig(selfieRequired: true)
            ..uploadError = const ApiException(code: 'FILE_TOO_LARGE', message: 'big', status: 422),
          FakeLocation(),
          fail,);
      await c2
          .read(checkControllerProvider.notifier)
          .submit(checkOut: false, confirmRationale: () async => true);
      expect(fail.discarded, ['/tmp/b.jpg']);
      expect(c2.read(checkControllerProvider).selfiePath, isNull);
    });

    test('check-out calls the check-out endpoint', () async {
      final repo = FakeAttendanceRepo();
      final c = _container(repo, FakeLocation(), FakeSelfie());
      final ok = await c
          .read(checkControllerProvider.notifier)
          .submit(checkOut: true, confirmRationale: () async => true);
      expect(ok, isTrue);
      expect(repo.checkOuts, 1);
      expect(c.read(checkControllerProvider).success, startsWith('Checked out at'));
    });
  });
}
