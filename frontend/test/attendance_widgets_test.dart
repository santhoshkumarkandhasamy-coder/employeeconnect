import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/attendance/data/attendance_repository.dart';
import 'package:employee_connect/features/attendance/data/location_service.dart';
import 'package:employee_connect/features/attendance/data/selfie_service.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart';
import 'package:employee_connect/features/attendance/presentation/attendance_screen.dart';
import 'package:employee_connect/features/attendance/presentation/widgets/month_grid.dart';
import 'package:employee_connect/features/attendance/presentation/widgets/today_status_card.dart';
import 'package:employee_connect/features/late/data/late_repository.dart';
import 'package:employee_connect/features/late/domain/late_models.dart';
import 'package:employee_connect/features/late/presentation/late_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/attendance_fakes.dart';

Widget _wrap(Widget child, List<Override> overrides, {ThemeMode mode = ThemeMode.light}) =>
    ProviderScope(
      overrides: overrides,
      child: MaterialApp(
        themeMode: mode,
        theme: ThemeData(useMaterial3: true),
        darkTheme: ThemeData(useMaterial3: true, brightness: Brightness.dark),
        home: child,
      ),
    );

List<Override> _att(FakeAttendanceRepo repo, {FakeLocation? loc, FakeSelfie? selfie}) => [
      attendanceRepositoryProvider.overrideWithValue(repo),
      locationServiceProvider.overrideWithValue(loc ?? FakeLocation()),
      selfieServiceProvider.overrideWithValue(selfie ?? FakeSelfie()),
    ];

void main() {
  testWidgets('TodayStatusCard shows each state', (tester) async {
    final repo = FakeAttendanceRepo();
    await tester.pumpWidget(_wrap(const Scaffold(body: TodayStatusCard()), _att(repo)));
    await tester.pumpAndSettle();
    expect(find.text('Not checked in yet'), findsOneWidget);

  });

  testWidgets('TodayStatusCard shows late check-in', (tester) async {
    final now = DateTime.now();
    final key = '${now.year.toString().padLeft(4, '0')}-'
        '${now.month.toString().padLeft(2, '0')}-${now.day.toString().padLeft(2, '0')}';
    final repo = FakeAttendanceRepo()
      ..todayRecord = AttendanceRecord(
        id: 'r',
        workDate: key,
        status: DayStatus.late,
        minutesLate: 12,
        checkInAt: DateTime(now.year, now.month, now.day, 9, 42),
      );
    await tester.pumpWidget(_wrap(const Scaffold(body: TodayStatusCard()), _att(repo)));
    await tester.pumpAndSettle();
    expect(find.text('Checked in at 09:42 (12 min late)'), findsOneWidget);
  });

  testWidgets('MonthGrid renders days, reports taps, works in dark mode', (tester) async {
    final handle = tester.ensureSemantics();
    CalendarDay? tapped;
    final days = [
      CalendarDay(date: DateTime(2026, 10, 1), status: DayStatus.holiday, holidayName: 'Festival'),
      CalendarDay(date: DateTime(2026, 10, 2), status: DayStatus.late),
    ];
    await tester.pumpWidget(_wrap(
      Scaffold(
        body: MonthGrid(
          month: '2026-10',
          days: days,
          today: DateTime(2026, 10, 21),
          onDayTap: (d) => tapped = d,
        ),
      ),
      const [],
      mode: ThemeMode.dark,
    ),);
    expect(find.text('31'), findsOneWidget);
    expect(find.text('Mon'), findsOneWidget);
    await tester.tap(find.text('2'));
    expect(tapped?.status, DayStatus.late);
    expect(find.bySemanticsLabel(RegExp('Thu, 1 Oct 2026, Holiday, Festival')), findsOneWidget);
    handle.dispose();
  });

  testWidgets('check-in screen shows OUTSIDE_GEOFENCE without a distance', (tester) async {
    final repo = FakeAttendanceRepo()
      ..checkInError = const ApiException(
        code: 'OUTSIDE_GEOFENCE',
        message: 'Outside',
        status: 422,
        details: [FieldError('within_fence', 'false')],
      );
    await tester.pumpWidget(_wrap(const AttendanceScreen(), _att(repo)));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Check in'));
    await tester.pumpAndSettle();

    expect(find.textContaining('outside the office area'), findsWidgets);
    expect(find.textContaining('340 m'), findsNothing);
  });

  testWidgets('selfie section reflects required config', (tester) async {
    final repo = FakeAttendanceRepo()..cfg = const AttendanceConfig(selfieRequired: true);
    await tester.pumpWidget(_wrap(const AttendanceScreen(), _att(repo)));
    await tester.pumpAndSettle();
    expect(find.text('Selfie required'), findsOneWidget);
  });

  testWidgets('history tab: empty state and error with retry', (tester) async {
    final repo = FakeAttendanceRepo();
    await tester.pumpWidget(_wrap(const AttendanceScreen(), _att(repo)));
    await tester.pumpAndSettle();
    await tester.tap(find.text('History'));
    await tester.pumpAndSettle();
    expect(find.text('No attendance records yet.'), findsOneWidget);
  });

  testWidgets('history tab lists records', (tester) async {
    final repo = FakeAttendanceRepo()
      ..historyItems = [
        AttendanceRecord(
          id: 'a',
          workDate: '2026-10-20',
          status: DayStatus.late,
          minutesLate: 15,
          checkInAt: DateTime(2026, 10, 20, 9, 45),
          checkOutAt: DateTime(2026, 10, 20, 18, 30),
          workedMinutes: 525,
        ),
      ];
    await tester.pumpWidget(_wrap(const AttendanceScreen(), _att(repo)));
    await tester.pumpAndSettle();
    await tester.tap(find.text('History'));
    await tester.pumpAndSettle();
    expect(find.text('Tue, 20 Oct 2026'), findsOneWidget);
    expect(find.textContaining('15 min late'), findsWidgets);
  });

  group('LateScreen', () {
    Widget app(FakeLateRepo repo) =>
        _wrap(const LateScreen(), [lateRepositoryProvider.overrideWithValue(repo)]);

    testWidgets('empty month shows zeros and empty message', (tester) async {
      await tester.pumpWidget(app(FakeLateRepo()));
      await tester.pumpAndSettle();
      expect(find.textContaining('No late arrivals'), findsOneWidget);
      expect(find.text('0'), findsOneWidget);
    });

    testWidgets('lists late days with minutes and summary', (tester) async {
      final repo = FakeLateRepo()
        ..records = [
          LateRecord(
            id: '1',
            workDate: '2026-10-05',
            minutesLate: 22,
            checkInAt: DateTime(2026, 10, 5, 9, 52),
            shiftStart: '09:30',
          ),
        ]
        ..sum = const LateSummary(
          month: '2026-10',
          lateCount: 1,
          totalMinutesLate: 22,
          averageMinutesLate: 22,
        );
      await tester.pumpWidget(app(repo));
      await tester.pumpAndSettle();
      expect(find.text('22 min late'), findsOneWidget);
      expect(find.text('22 min'), findsOneWidget); // average
      expect(find.text('22m'), findsOneWidget); // total
    });

    testWidgets('error state offers retry', (tester) async {
      final repo = FakeLateRepo()
        ..error = const ApiException(code: ApiException.networkCode, message: 'No connection.');
      await tester.pumpWidget(app(repo));
      await tester.pumpAndSettle();
      expect(find.text('No connection.'), findsOneWidget);
      repo.error = null;
      await tester.tap(find.text('Try again'));
      await tester.pumpAndSettle();
      expect(find.textContaining('No late arrivals'), findsOneWidget);
    });
  });
}
