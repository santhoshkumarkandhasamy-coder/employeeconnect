import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:employee_connect/core/cache/json_cache.dart';
import 'package:employee_connect/features/home/data/home_repository.dart';
import 'package:employee_connect/features/home/domain/home_models.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/module_fakes.dart';

void main() {
  final now = DateTime(2026, 10, 21, 10);

  group('HomeData.fromJson', () {
    test('parses a full response', () {
      final d = sampleHome(now: now);
      expect(d.attendance!.checkInAt, isNotNull);
      expect(d.leaveBalances!.map((e) => e.type), ['Casual', 'Sick']);
      expect(d.leaveBalances!.last.remaining, 7.5);
      expect(d.permission!.label, '2 left, 3h');
      expect(d.unreadAnnouncements, 3);
      expect(d.pendingRequests, 1);
      expect(d.food!.date, '2026-10-21');
      expect(d.food!.needFood, isNull);
      expect(d.food!.canChange, isTrue);
      expect(d.isEmpty, isFalse);
    });

    test('tolerates every part being null (the server nulls failed parts)', () {
      final d = HomeData.fromJson({
        'today_attendance': null,
        'leave_balances': null,
        'permission_remaining': null,
        'unread_announcements': null,
        'pending_requests': null,
        'food_today': null,
        'celebrations': null,
      });
      expect(d.attendance, isNull);
      expect(d.leaveBalances, isNull);
      expect(d.permission, isNull);
      expect(d.food, isNull);
      expect(d.celebrations, isEmpty);
      expect(d.isEmpty, isTrue);
    });

    test('an empty object parses too', () {
      expect(HomeData.fromJson(const {}).isEmpty, isTrue);
    });

    test('skips malformed celebrations', () {
      final d = HomeData.fromJson({
        'celebrations': [
          {'user': null, 'type': 'birthday', 'date': '2026-10-21'},
          {'user': {'id': '1', 'employee_id': 'E1', 'full_name': 'A'}, 'type': 'birthday', 'date': 'bad'},
          {'user': {'id': '2', 'employee_id': 'E2', 'full_name': 'B'}, 'type': 'mystery', 'date': '2026-10-22'},
          'junk',
        ],
      });
      expect(d.celebrations.single.user.fullName, 'B');
      expect(d.celebrations.single.type, CelebrationType.birthday);
    });
  });

  group('celebrations', () {
    test('sorted by date then name, today first', () {
      final list = sampleHome(now: now).celebrations;
      expect(list.map((c) => c.user.fullName), ['Meera Nair', 'Ravi Kumar']);
      expect(list.first.isToday(now), isTrue);
    });

    test('labels', () {
      final c = sampleHome(now: now).celebrations;
      expect(c[0].whenLabel(now), 'Today');
      expect(c[1].whenLabel(now), 'Fri 23 Oct');
      expect(c[0].typeLabel, 'Birthday');
      expect(c[1].typeLabel, 'Anniversary');
      final tomorrow = Celebration.tryParse({
        'user': {'id': '1', 'employee_id': 'E', 'full_name': 'X'},
        'type': 'birthday',
        'date': '2026-10-22',
      })!;
      expect(tomorrow.whenLabel(now), 'Tomorrow');
    });

    test('wish messages use the first name and never a year of birth', () {
      final c = sampleHome(now: now).celebrations;
      expect(c[0].wish(), startsWith('Happy birthday, Meera!'));
      expect(c[1].wish(), startsWith('Happy anniversary, Ravi!'));
    });
  });

  group('small helpers', () {
    test('greeting by hour', () {
      expect(greeting(DateTime(2026, 1, 1, 8)), 'Good morning');
      expect(greeting(DateTime(2026, 1, 1, 13)), 'Good afternoon');
      expect(greeting(DateTime(2026, 1, 1, 19)), 'Good evening');
    });

    test('attendance text', () {
      expect(const HomeAttendance().text, 'Not checked in yet');
      final inOnly = HomeAttendance(checkInAt: DateTime(2026, 10, 21, 9, 5), status: 'late');
      expect(inOnly.text, 'Checked in at 09:05 (late)');
      final both = HomeAttendance(
        checkInAt: DateTime(2026, 10, 21, 9, 5),
        checkOutAt: DateTime(2026, 10, 21, 18, 0),
      );
      expect(both.text, 'In 09:05, out 18:00');
    });

    test('food copyWith', () {
      const f = HomeFood(date: '2026-10-21', canChange: true);
      expect(f.copyWith(needFood: true).needFood, isTrue);
      expect(f.copyWith(needFood: true).date, '2026-10-21');
    });
  });

  group('HomeRepository', () {
    test('falls back to the cached dashboard when offline', () async {
      var offline = false;
      final dio = Dio(BaseOptions(baseUrl: 'https://x.test/api'))
        ..httpClientAdapter = _Adapter(() => offline);
      final repo = HomeRepository(dio, MemoryJsonCache(), scope: 'u1');
      final live = await repo.home();
      expect(live.stale, isFalse);
      expect(live.value.unreadAnnouncements, 3);

      offline = true;
      final cached = await repo.home();
      expect(cached.stale, isTrue);
      expect(cached.value.unreadAnnouncements, 3);
      expect(cached.value.leaveBalances!.length, 2);
      // Colleagues' names and birthdays are scrubbed from the disk cache (review 07 L8).
      expect(live.value.celebrations.single.user.fullName, 'Meera Nair');
      expect(cached.value.celebrations, isEmpty);
    });

    test('scrub drops celebrations and announcement bodies', () {
      final out = scrubSensitive({
        'celebrations': [
          {'user': {'full_name': 'X'}},
        ],
        'items': [
          {'id': 'a1', 'title': 'T', 'body': 'secret'},
        ],
      }) as Map;
      expect(out.containsKey('celebrations'), isFalse);
      final item = (out['items'] as List).single as Map;
      expect(item.containsKey('body'), isFalse);
      expect(item['title'], 'T');
    });
  });
}

class _Adapter implements HttpClientAdapter {
  _Adapter(this.offline);
  final bool Function() offline;

  @override
  Future<ResponseBody> fetch(RequestOptions options, Stream<Uint8List>? requestStream, Future<void>? cancelFuture) async {
    if (offline()) throw DioException.connectionError(requestOptions: options, reason: 'offline');
    expectPath(options.path);
    return ResponseBody.fromString(
      jsonEncode({
        'unread_announcements': 3,
        'leave_balances': [
          {'leave_type': 'Casual', 'remaining': 8.0},
          {'leave_type': 'Sick', 'remaining': 7.5},
        ],
        'celebrations': [
          {
            'user': {'id': 'c1', 'employee_id': 'E1', 'full_name': 'Meera Nair'},
            'type': 'birthday',
            'date': '2026-10-21',
          },
        ],
      }),
      200,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );
  }

  void expectPath(String path) => expect(path, '/home');

  @override
  void close({bool force = false}) {}
}
