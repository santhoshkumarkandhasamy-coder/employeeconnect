import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/cache/json_cache.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart' show Paged;
import 'package:employee_connect/features/auth/domain/auth_user.dart';
import 'package:employee_connect/features/auth/presentation/auth_controller.dart';
import 'package:employee_connect/features/auth/presentation/auth_state.dart';
import 'package:employee_connect/features/leave/data/leave_repository.dart';
import 'package:employee_connect/features/leave/domain/leave_models.dart';
import 'package:employee_connect/features/leave/presentation/approvals_screen.dart';
import 'package:employee_connect/features/leave/presentation/leave_screen.dart';
import 'package:employee_connect/features/leave/presentation/widgets/request_widgets.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

class FakeLeaveRepo extends LeaveRepository {
  FakeLeaveRepo() : super(Dio(), MemoryJsonCache());

  List<LeaveType> typeList = const [
    LeaveType(id: 't1', code: 'CL', name: 'Casual Leave', allowHalfDay: true),
  ];
  BalanceData bal = const BalanceData(
    year: 2026,
    leave: [
      LeaveBalanceItem(
        leaveTypeId: 't1',
        code: 'CL',
        name: 'Casual Leave',
        quota: 12,
        used: 3,
        pending: 1,
        remaining: 8,
      ),
    ],
    permission: PermissionBalance(
      month: '2026-10',
      limitCount: 3,
      usedCount: 1,
      pendingCount: 1,
      remainingCount: 1,
      limitMinutes: 240,
      usedMinutes: 60,
      pendingMinutes: 30,
      remainingMinutes: 150,
    ),
  );
  List<RequestItem> mine = [];
  List<RequestItem> adminItems = [];
  ApiException? balanceError;
  ApiException? decideError;
  final List<(RequestKind, String)> cancelled = [];
  final List<({String id, bool approve, String? comment})> decisions = [];

  @override
  Future<Cached<List<LeaveType>>> types() async => Cached(typeList);

  @override
  Future<Cached<BalanceData>> balance(int year) async {
    final e = balanceError;
    if (e != null) throw e;
    return Cached(bal);
  }

  @override
  Future<Cached<List<RequestItem>>> myRequests() async => Cached(mine);

  @override
  Future<RequestItem> cancel(RequestKind kind, String id) async {
    cancelled.add((kind, id));
    mine = [
      for (final i in mine)
        if (i.id == id)
          RequestItem(kind: i.kind, id: i.id, status: RequestStatus.cancelled, leaveTypeName: i.leaveTypeName,
              fromDate: i.fromDate, toDate: i.toDate, days: i.days,)
        else
          i,
    ];
    return mine.first;
  }

  @override
  Future<Paged<RequestItem>> adminRequests({
    required RequestKind kind,
    RequestStatus? status,
    int page = 1,
  }) async =>
      Paged(items: adminItems, page: 1, total: adminItems.length, pageSize: 20);

  @override
  Future<RequestItem> decide(RequestKind kind, String id, {required bool approve, String? comment}) async {
    decisions.add((id: id, approve: approve, comment: comment));
    final e = decideError;
    if (e != null) throw e;
    return adminItems.first;
  }
}

class _FakeAuth extends AuthController {
  _FakeAuth(this.user);
  final AuthUser user;
  @override
  AuthState build() => AuthAuthenticated(user);
}

Widget _wrap(Widget child, FakeLeaveRepo repo, {AuthUser? user, ThemeMode mode = ThemeMode.light}) =>
    ProviderScope(
      overrides: [
        leaveRepositoryProvider.overrideWithValue(repo),
        if (user != null) authControllerProvider.overrideWith(() => _FakeAuth(user)),
      ],
      child: MaterialApp(
        themeMode: mode,
        theme: ThemeData(useMaterial3: true),
        darkTheme: ThemeData(useMaterial3: true, brightness: Brightness.dark),
        home: child,
      ),
    );

RequestItem _leave({String id = 'r1', RequestStatus status = RequestStatus.pending, PersonSummary? user}) =>
    RequestItem(
      kind: RequestKind.leave,
      id: id,
      status: status,
      leaveTypeName: 'Casual Leave',
      fromDate: '2026-11-03',
      toDate: '2026-11-05',
      days: 3,
      reason: 'Family function',
      user: user,
    );

const _manager = AuthUser(
  id: 'm1',
  employeeId: 'EMP010',
  fullName: 'Mgr',
  role: 'manager',
  permissions: ['request.approve_team'],
  mustChangePassword: false,
);

void main() {
  testWidgets('balance tab shows leave remaining and permission incl. pending', (tester) async {
    await tester.pumpWidget(_wrap(const LeaveScreen(), FakeLeaveRepo()));
    await tester.pumpAndSettle();
    expect(find.text('Casual Leave'), findsOneWidget);
    expect(find.text('8 left'), findsOneWidget);
    expect(find.text('Pending 1'), findsOneWidget);
    expect(find.text('1 of 3 requests left'), findsOneWidget);
    expect(find.textContaining('Includes pending: 1 requests, 30m'), findsOneWidget);
  });

  testWidgets('balance tab shows error and retries', (tester) async {
    final repo = FakeLeaveRepo()
      ..balanceError = const ApiException(code: ApiException.networkCode, message: 'No connection.');
    await tester.pumpWidget(_wrap(const LeaveScreen(), repo, mode: ThemeMode.dark));
    await tester.pumpAndSettle();
    expect(find.text('No connection.'), findsOneWidget);
    repo.balanceError = null;
    await tester.tap(find.text('Try again'));
    await tester.pumpAndSettle();
    expect(find.text('8 left'), findsOneWidget);
  });

  testWidgets('request tab validates before submitting', (tester) async {
    await tester.pumpWidget(_wrap(const LeaveScreen(initialTab: 1), FakeLeaveRepo()));
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('Submit leave request'));
    await tester.tap(find.text('Submit leave request'));
    await tester.pumpAndSettle();
    expect(find.text('Choose a leave type'), findsOneWidget);
    expect(find.text('Enter a reason'), findsOneWidget);
  });

  testWidgets('request tab switches to permission form', (tester) async {
    await tester.pumpWidget(_wrap(const LeaveScreen(initialTab: 1), FakeLeaveRepo()));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Permission'));
    await tester.pumpAndSettle();
    expect(find.text('From time'), findsOneWidget);
    expect(find.text('Submit permission request'), findsOneWidget);
  });

  testWidgets('my requests: empty state', (tester) async {
    await tester.pumpWidget(_wrap(const LeaveScreen(initialTab: 2), FakeLeaveRepo()));
    await tester.pumpAndSettle();
    expect(find.textContaining('No requests yet'), findsOneWidget);
  });

  testWidgets('my requests: filter chips and status chip', (tester) async {
    final repo = FakeLeaveRepo()
      ..mine = [_leave(), _leave(id: 'r2', status: RequestStatus.approved)];
    await tester.pumpWidget(_wrap(const LeaveScreen(initialTab: 2), repo));
    await tester.pumpAndSettle();
    expect(find.byType(RequestTile), findsNWidgets(2));
    await tester.tap(find.widgetWithText(ChoiceChip, 'Approved'));
    await tester.pumpAndSettle();
    expect(find.byType(RequestTile), findsOneWidget);
    await tester.tap(find.widgetWithText(ChoiceChip, 'Rejected'));
    await tester.pumpAndSettle();
    expect(find.text('No rejected requests.'), findsOneWidget);
  });

  testWidgets('cancel a pending request asks for confirmation', (tester) async {
    final repo = FakeLeaveRepo()..mine = [_leave()];
    await tester.pumpWidget(_wrap(const LeaveScreen(initialTab: 2), repo));
    await tester.pumpAndSettle();
    await tester.tap(find.byType(RequestTile));
    await tester.pumpAndSettle();
    expect(find.text('Family function'), findsOneWidget);
    await tester.tap(find.text('Cancel request'));
    await tester.pumpAndSettle();
    expect(find.text('Cancel this request?'), findsOneWidget);
    // Keep it: nothing cancelled.
    await tester.tap(find.text('Keep'));
    await tester.pumpAndSettle();
    expect(repo.cancelled, isEmpty);

    await tester.tap(find.byType(RequestTile));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Cancel request'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Cancel request'));
    await tester.pumpAndSettle();
    expect(repo.cancelled, [(RequestKind.leave, 'r1')]);
  });

  testWidgets('approved request offers no cancel button', (tester) async {
    final repo = FakeLeaveRepo()..mine = [_leave(status: RequestStatus.approved)];
    await tester.pumpWidget(_wrap(const LeaveScreen(initialTab: 2), repo));
    await tester.pumpAndSettle();
    await tester.tap(find.byType(RequestTile));
    await tester.pumpAndSettle();
    expect(find.text('Cancel request'), findsNothing);
  });

  group('ApprovalsScreen', () {
    const emp = PersonSummary(id: 'u7', employeeId: 'EMP007', fullName: 'Ravi K');

    testWidgets('empty state', (tester) async {
      await tester.pumpWidget(_wrap(const ApprovalsScreen(), FakeLeaveRepo(), user: _manager));
      await tester.pumpAndSettle();
      expect(find.text('Nothing waiting for approval.'), findsOneWidget);
      expect(find.text('Showing: Your team'), findsOneWidget);
    });

    testWidgets('reject requires a comment', (tester) async {
      final repo = FakeLeaveRepo()..adminItems = [_leave(user: emp)];
      await tester.pumpWidget(_wrap(const ApprovalsScreen(), repo, user: _manager));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(RequestTile));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Reject'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(FilledButton, 'Reject'));
      await tester.pumpAndSettle();
      expect(find.text('A comment is required to reject'), findsOneWidget);
      expect(repo.decisions, isEmpty);

      await tester.enterText(find.byType(TextFormField), 'Team is short');
      await tester.tap(find.widgetWithText(FilledButton, 'Reject'));
      await tester.pumpAndSettle();
      expect(repo.decisions.single.approve, isFalse);
      expect(repo.decisions.single.comment, 'Team is short');
    });

    testWidgets('approve works without a comment and removes the item', (tester) async {
      final repo = FakeLeaveRepo()..adminItems = [_leave(user: emp)];
      await tester.pumpWidget(_wrap(const ApprovalsScreen(), repo, user: _manager));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(RequestTile));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Approve'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(FilledButton, 'Approve'));
      await tester.pumpAndSettle();
      expect(repo.decisions.single.approve, isTrue);
      expect(find.byType(RequestTile), findsNothing);
    });

    testWidgets('own request cannot be approved or rejected', (tester) async {
      final repo = FakeLeaveRepo()
        ..adminItems = [_leave(user: const PersonSummary(id: 'm1', employeeId: 'EMP010', fullName: 'Mgr'))];
      await tester.pumpWidget(_wrap(const ApprovalsScreen(), repo, user: _manager));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(RequestTile));
      await tester.pumpAndSettle();
      expect(find.text('Approve'), findsNothing);
      expect(find.textContaining('cannot approve or reject your own'), findsOneWidget);
    });

    testWidgets('view-only users get no decision buttons', (tester) async {
      const viewer = AuthUser(
        id: 'v1',
        employeeId: 'EMP020',
        fullName: 'Viewer',
        role: 'manager',
        permissions: ['request.view_team'],
        mustChangePassword: false,
      );
      final repo = FakeLeaveRepo()..adminItems = [_leave(user: emp)];
      await tester.pumpWidget(_wrap(const ApprovalsScreen(), repo, user: viewer));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(RequestTile));
      await tester.pumpAndSettle();
      expect(find.text('Approve'), findsNothing);
    });

    testWidgets('NOT_PENDING error is shown in friendly words', (tester) async {
      final repo = FakeLeaveRepo()
        ..adminItems = [_leave(user: emp)]
        ..decideError = const ApiException(code: 'NOT_PENDING', message: 'x', status: 409);
      await tester.pumpWidget(_wrap(const ApprovalsScreen(), repo, user: _manager));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(RequestTile));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Approve'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(FilledButton, 'Approve'));
      await tester.pumpAndSettle();
      expect(find.text('This request is no longer pending.'), findsOneWidget);
    });
  });
}
