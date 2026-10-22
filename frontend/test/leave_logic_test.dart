import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/cache/json_cache.dart';
import 'package:employee_connect/features/leave/data/leave_repository.dart';
import 'package:employee_connect/features/leave/domain/leave_logic.dart';
import 'package:employee_connect/features/leave/domain/leave_models.dart';
import 'package:flutter_test/flutter_test.dart';

const _cl = LeaveType(id: 't1', code: 'CL', name: 'Casual Leave', allowHalfDay: true);
const _sl = LeaveType(
  id: 't2',
  code: 'SL',
  name: 'Sick Leave',
  requiresAttachmentAfterDays: 2,
  maxConsecutiveDays: 10,
);

void main() {
  group('year splitting', () {
    test('single-year range is not split', () {
      final r = splitByYear(DateTime(2026, 11, 3), DateTime(2026, 11, 5));
      expect(r, hasLength(1));
      expect(crossesYear(DateTime(2026, 11, 3), DateTime(2026, 11, 5)), isFalse);
    });

    test('range crossing new year splits at 31 Dec', () {
      final r = splitByYear(DateTime(2026, 12, 28), DateTime(2027, 1, 3));
      expect(r, hasLength(2));
      expect(r[0].from, DateTime(2026, 12, 28));
      expect(r[0].to, DateTime(2026, 12, 31));
      expect(r[1].from, DateTime(2027, 1, 1));
      expect(r[1].to, DateTime(2027, 1, 3));
    });

    test('reversed range yields nothing; message names both parts', () {
      expect(splitByYear(DateTime(2026, 5, 2), DateTime(2026, 5, 1)), isEmpty);
      expect(
        yearCrossingMessage(DateTime(2026, 12, 30), DateTime(2027, 1, 2)),
        contains('2026-12-30 to 2026-12-31 and 2027-01-01 to 2027-01-02'),
      );
    });

    test('calendarSpan is inclusive', () {
      expect(calendarSpan(DateTime(2026, 11, 3), DateTime(2026, 11, 5)), 3);
      expect(calendarSpan(DateTime(2026, 11, 3), DateTime(2026, 11, 3)), 1);
    });
  });

  group('validateLeaveForm', () {
    String? v({
      LeaveType? type = _cl,
      DateTime? from,
      DateTime? to,
      bool half = false,
      String? session = 'first',
      String reason = 'Family function',
    }) =>
        validateLeaveForm(
          type: type,
          from: from ?? DateTime(2026, 11, 3),
          to: to ?? DateTime(2026, 11, 3),
          halfDay: half,
          halfDaySession: session,
          reason: reason,
        );

    test('valid form passes', () => expect(v(), isNull));
    test('requires type, dates and reason', () {
      expect(v(type: null), 'Choose a leave type');
      expect(v(reason: '  '), 'Enter a reason');
      expect(
        validateLeaveForm(
          type: _cl,
          from: null,
          to: null,
          halfDay: false,
          halfDaySession: null,
          reason: 'x',
        ),
        isNotNull,
      );
    });
    test('to before from is rejected', () {
      expect(v(from: DateTime(2026, 11, 5), to: DateTime(2026, 11, 3)), contains('on or after'));
    });
    test('half day rules', () {
      expect(v(half: true), isNull);
      expect(v(type: _sl, half: true), contains('does not allow half days'));
      expect(v(half: true, to: DateTime(2026, 11, 4)), contains('single date'));
      expect(v(half: true, session: null), contains('first or second'));
    });
    test('max consecutive days', () {
      expect(v(type: _sl, to: DateTime(2026, 11, 20)), contains('at most 10'));
    });
  });

  group('validatePermissionForm', () {
    final today = DateTime(2026, 10, 21);
    String? v({DateTime? date, String? from = '16:00', String? to = '17:30', String reason = 'Bank'}) =>
        validatePermissionForm(
          date: date ?? today,
          fromTime: from,
          toTime: to,
          reason: reason,
          today: today,
        );

    test('valid passes', () => expect(v(), isNull));
    test('past date rejected', () => expect(v(date: DateTime(2026, 10, 20)), contains('past')));
    test('to must be after from', () {
      expect(v(to: '16:00'), contains('after'));
      expect(v(to: '15:00'), contains('after'));
    });
    test('missing pieces', () {
      expect(v(from: null), isNotNull);
      expect(v(reason: ''), 'Enter a reason');
    });
    test('minutes helpers', () {
      expect(minutesBetween('16:00', '17:30'), 90);
      expect(parseHm('24:00'), isNull);
      expect(formatHm(9, 5), '09:05');
    });
  });

  group('leaveErrorMessage', () {
    ApiException e(String code, {List<FieldError> d = const []}) =>
        ApiException(code: code, message: 'server text', details: d);

    test('insufficient balance shows days left', () {
      expect(
        leaveErrorMessage(e('INSUFFICIENT_BALANCE', d: const [FieldError('remaining', '1.5')])),
        contains('1.5 days left'),
      );
    });
    test('permission limit names count or minutes', () {
      expect(leaveErrorMessage(e('PERMISSION_LIMIT_EXCEEDED', d: const [FieldError('count', '')])),
          contains('requests allowed'),);
      expect(leaveErrorMessage(e('PERMISSION_LIMIT_EXCEEDED', d: const [FieldError('minutes', '')])),
          contains('minutes'),);
    });
    test('every new domain code has friendly text', () {
      for (final c in [
        'OVERLAPPING_REQUEST',
        'NOT_PENDING',
        'ZERO_WORKING_DAYS',
        'ATTACHMENT_REQUIRED',
        'HALF_DAY_NOT_ALLOWED',
        'LEAVE_TYPE_INACTIVE',
        'NO_SHIFT_ASSIGNED',
        'FORBIDDEN',
        'NOT_FOUND',
        'REQUESTER_INACTIVE',
        'TEMPORARILY_UNAVAILABLE',
        'RATE_LIMITED',
        'INVALID_DATE_RANGE',
      ]) {
        expect(leaveErrorMessage(e(c)), isNot('server text'), reason: c);
      }
    });
    test('rate limit honours Retry-After', () {
      final m = leaveErrorMessage(const ApiException(
          code: 'RATE_LIMITED', message: 'x', status: 429, retryAfter: Duration(seconds: 30),),);
      expect(m, contains('30 seconds'));
      expect(waitText(const Duration(seconds: 90)), '2 minutes');
    });
    test('404 and 403 on decisions read differently', () {
      expect(leaveErrorMessage(e('NOT_FOUND')), contains('no longer yours'));
      expect(leaveErrorMessage(e('FORBIDDEN')), contains('own request'));
      expect(applyErrorMessage(e('NOT_FOUND')), contains('leave type'));
    });
    test('INVALID_DATE_RANGE mentions merged requests', () {
      expect(leaveErrorMessage(e('INVALID_DATE_RANGE')), contains('counted together'));
    });
    test('unknown code falls back to server message', () {
      expect(leaveErrorMessage(e('SOMETHING')), 'server text');
    });
  });

  group('push routing and permissions', () {
    test('payload types map to screens', () {
      expect(parseRequestPush({'type': 'new_request', 'id': 'x'})!.screen, RequestPushScreen.approvals);
      final d = parseRequestPush({'type': 'leave_decision', 'id': 'abc'})!;
      expect(d.screen, RequestPushScreen.myRequests);
      expect(d.kind, RequestKind.leave);
      expect(d.id, 'abc');
      expect(parseRequestPush({'type': 'permission_decision', 'id': 'p'})!.kind, RequestKind.permission);
      expect(parseRequestPush({'type': 'announcement'}), isNull);
    });

    test('approval gating by permission', () {
      expect(canOpenApprovals(const []), isFalse);
      expect(canOpenApprovals(const ['attendance.view_own']), isFalse);
      expect(canOpenApprovals(const ['request.view_team']), isTrue);
      expect(canDecideRequests(const ['request.view_team']), isFalse);
      expect(canDecideRequests(const ['request.approve_team']), isTrue);
      expect(approvalScopeLabel(const ['request.approve_team']), 'Your team');
      expect(approvalScopeLabel(const ['request.approve_team', 'request.approve_all']), 'All employees');
    });
  });

  group('models', () {
    test('formatDays', () {
      expect(formatDays(3), '3');
      expect(formatDays(0.5), '0.5');
      expect(daysLabel(1), '1 day');
      expect(daysLabel(2.5), '2.5 days');
    });

    test('balance parsing including permission pending fields', () {
      final b = BalanceData.fromJson({
        'year': 2026,
        'leave': [
          {'leave_type_id': 't1', 'code': 'CL', 'name': 'Casual', 'quota': 12.0, 'used': 3.0, 'pending': 1.0, 'remaining': 8.0},
        ],
        'permission': {
          'month': '2026-10',
          'limit_count': 3,
          'used_count': 1,
          'pending_count': 1,
          'remaining_count': 1,
          'limit_minutes': 240,
          'used_minutes': 60,
          'pending_minutes': 30,
          'remaining_minutes': 150,
        },
      });
      expect(b.forType('t1')!.remaining, 8.0);
      expect(b.permission.pendingMinutes, 30);
      expect(b.permission.remainingCount, 1);
    });

    test('request parsing: leave, permission and admin item', () {
      final l = RequestItem.fromJson({
        'id': '1',
        'leave_type': {'id': 't1', 'code': 'CL', 'name': 'Casual Leave'},
        'from_date': '2026-11-03',
        'to_date': '2026-11-05',
        'days': 3.0,
        'status': 'pending',
        'reason': 'r',
      }, kind: RequestKind.leave,);
      expect(l.title, 'Casual Leave');
      expect(l.amountText, '3 days');
      expect(l.whenText, 'Tue, 3 Nov 2026 to Thu, 5 Nov 2026');
      expect(l.isPending, isTrue);

      final p = RequestItem.fromJson({
        'id': '2',
        'type': 'permission',
        'date': '2026-11-03',
        'from_time': '16:00',
        'to_time': '17:30',
        'minutes': 90,
        'status': 'approved',
        'user': {'id': 'u9', 'employee_id': 'EMP009', 'full_name': 'B C'},
      });
      expect(p.kind, RequestKind.permission);
      expect(p.amountText, '1h 30m');
      expect(p.user!.display, 'B C');
      expect(p.status, RequestStatus.approved);
      expect(RequestStatus.parse('weird'), RequestStatus.unknown);
    });

    test('mergeRequests sorts newest first', () {
      final m = mergeRequests({
        'leave': {
          'items': [
            {'id': 'a', 'status': 'pending', 'created_at': '2026-10-01T00:00:00Z'},
          ],
        },
        'permission': {
          'items': [
            {'id': 'b', 'status': 'pending', 'created_at': '2026-10-05T00:00:00Z'},
          ],
        },
      });
      expect(m.map((e) => e.id), ['b', 'a']);
      expect(m.first.kind, RequestKind.permission);
    });
  });

  test('cache scrubs reasons, comments and attachment links', () {
    final out = scrubSensitive({
      'items': [
        {'id': '1', 'reason': 'medical', 'decision_comment': 'ok', 'attachment_url': '/api/files/1', 'days': 2},
      ],
    }) as Map;
    final item = (out['items'] as List).first as Map;
    expect(item.keys, unorderedEquals(['id', 'days']));
  });

  test('myRequests merge + offline cache fallback', () async {
    final cache = MemoryJsonCache();
    final repo = _FlakyRepo(cache);
    final first = await repo.myRequests();
    expect(first.stale, isFalse);
    expect(first.value, hasLength(2));
    repo.offline = true;
    final second = await repo.myRequests();
    expect(second.stale, isTrue);
    expect(second.value, hasLength(2));
  });

  group('limits, sniffing and parsing', () {
    test('picker cap and balance year range', () {
      expect(leaveLastDate(DateTime(2026, 10, 21)), DateTime(2027, 10, 21));
      expect(isValidBalanceYear(1999), isFalse);
      expect(isValidBalanceYear(2000), isTrue);
      expect(isValidBalanceYear(2200), isTrue);
      expect(isValidBalanceYear(2201), isFalse);
    });
    test('balance outside 2000-2200 fails with 422 before any request', () {
      final repo = LeaveRepository(Dio(), MemoryJsonCache());
      expect(() => repo.balance(2500), throwsA(isA<ApiException>().having((e) => e.status, 'status', 422)));
    });
    test('sniffContentType', () {
      expect(sniffContentType([0x25, 0x50, 0x44, 0x46, 1]), 'application/pdf');
      expect(sniffContentType([0xFF, 0xD8, 0xFF, 0]), 'image/jpeg');
      expect(sniffContentType([1, 2, 3, 4], declared: 'image/png'), 'image/png');
      expect(sniffContentType([1, 2, 3, 4]), isNull);
    });
    test('empty reason and purged attachment are tolerated', () {
      final i = RequestItem.fromJson({
        'id': '1',
        'status': 'approved',
        'reason': '',
        'attachment_url': null,
        'attachment_content_type': null,
        'days': 1,
      }, kind: RequestKind.leave,);
      expect(i.reason, '');
      expect(i.hasAttachment, isFalse);
      final j = RequestItem.fromJson({
        'id': '2',
        'status': 'pending',
        'attachment_url': '/api/files/abcdef12-3456',
        'attachment_content_type': 'application/pdf',
      }, kind: RequestKind.leave,);
      expect(j.hasAttachment, isTrue);
      expect(j.attachmentContentType, 'application/pdf');
    });
  });
}

class _FlakyRepo extends LeaveRepository {
  _FlakyRepo(this.store) : super(Dio(), store);
  final JsonCache store;
  bool offline = false;

  @override
  Future<Cached<List<RequestItem>>> myRequests() => fetchWithCache(
        cache: store,
        key: 'k',
        fetch: () async {
          if (offline) throw const ApiException(code: ApiException.networkCode, message: 'offline');
          return <String, dynamic>{
            'leave': {
              'items': [
                {'id': 'a', 'status': 'pending'},
              ],
            },
            'permission': {
              'items': [
                {'id': 'b', 'status': 'pending'},
              ],
            },
          };
        },
        parse: (j) => mergeRequests(j as Map<String, dynamic>),
      );
}
