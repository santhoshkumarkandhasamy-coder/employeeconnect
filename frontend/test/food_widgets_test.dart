import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/food/data/food_repository.dart';
import 'package:employee_connect/features/food/domain/food_models.dart';
import 'package:employee_connect/features/food/presentation/food_providers.dart';
import 'package:employee_connect/features/food/presentation/food_report_screen.dart';
import 'package:employee_connect/features/food/presentation/food_screen.dart';
import 'package:employee_connect/features/leave/domain/leave_models.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/food_fakes.dart';

final _now = DateTime(2026, 10, 21, 12);

Widget _wrap(Widget child, FakeFoodRepo repo, {ThemeMode mode = ThemeMode.light}) => ProviderScope(
      overrides: [
        foodRepositoryProvider.overrideWithValue(repo),
        foodClockProvider.overrideWithValue(() => _now),
      ],
      child: MaterialApp(
        themeMode: mode,
        theme: ThemeData(useMaterial3: true),
        darkTheme: ThemeData(useMaterial3: true, brightness: Brightness.dark),
        home: child,
      ),
    );

List<FoodDay> _days() => [
      FoodDay(date: '2026-10-21', needFood: true, cutoffAt: DateTime(2026, 10, 20, 18)), // locked
      FoodDay(date: '2026-10-22', needFood: false, cutoffAt: DateTime(2026, 10, 21, 18)),
      const FoodDay(date: '2026-10-23', isHoliday: true),
    ];

Switch _sw(WidgetTester t, String date) =>
    t.widget<Switch>(find.byKey(ValueKey('food-switch-$date')));

void main() {
  testWidgets('shows days, locked, holiday and cut-off text', (tester) async {
    await tester.pumpWidget(_wrap(const FoodScreen(), FakeFoodRepo(_days())));
    await tester.pumpAndSettle();
    expect(find.textContaining('Locked - cut-off was 6:00 PM'), findsOneWidget);
    expect(find.textContaining('Change within 6h'), findsOneWidget);
    expect(find.textContaining('Holiday or day off'), findsOneWidget);
    expect(_sw(tester, '2026-10-21').onChanged, isNull);
    expect(_sw(tester, '2026-10-22').onChanged, isNotNull);
    expect(find.byKey(const ValueKey('food-switch-2026-10-23')), findsNothing);
  });

  testWidgets('toggle updates optimistically and saves', (tester) async {
    final repo = FakeFoodRepo(_days());
    await tester.pumpWidget(_wrap(const FoodScreen(), repo));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('food-switch-2026-10-22')));
    await tester.pump(); // optimistic frame, save still pending
    expect(_sw(tester, '2026-10-22').value, isTrue);
    await tester.pumpAndSettle();
    expect(repo.sets, [('2026-10-22', true)]);
    expect(_sw(tester, '2026-10-22').value, isTrue);
  });

  testWidgets('failure rolls back and shows a mapped message', (tester) async {
    final repo = FakeFoodRepo(_days())
      ..setError = const ApiException(code: 'FOOD_CUTOFF_PASSED', message: 'x', status: 409);
    await tester.pumpWidget(_wrap(const FoodScreen(), repo));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('food-switch-2026-10-22')));
    await tester.pumpAndSettle();
    expect(_sw(tester, '2026-10-22').value, isFalse);
    expect(find.textContaining('cut-off time has passed'), findsOneWidget);
  });

  testWidgets('bulk sets only open days', (tester) async {
    final repo = FakeFoodRepo(_days());
    await tester.pumpWidget(_wrap(const FoodScreen(), repo));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Set for the week'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Yes'));
    await tester.pumpAndSettle();
    expect(repo.bulks.single, ['2026-10-22']);
  });

  testWidgets('empty state', (tester) async {
    await tester.pumpWidget(_wrap(const FoodScreen(), FakeFoodRepo([])));
    await tester.pumpAndSettle();
    expect(find.textContaining('No days are open'), findsOneWidget);
  });

  testWidgets('error state in dark mode has retry', (tester) async {
    final repo = FakeFoodRepo([])
      ..loadError = const ApiException(code: ApiException.networkCode, message: 'No connection.');
    await tester.pumpWidget(_wrap(const FoodScreen(), repo, mode: ThemeMode.dark));
    await tester.pumpAndSettle();
    expect(find.text('No connection.'), findsOneWidget);
    expect(find.text('Try again'), findsOneWidget);
  });

  testWidgets('report groups employees by department', (tester) async {
    final repo = FakeFoodRepo([])
      ..report = const FoodDailyReport(
        date: '2026-10-21',
        countYes: 2,
        countNo: 1,
        countUnanswered: 4,
        employees: [
          FoodEmployee(
            person: PersonSummary(id: '1', employeeId: 'E1', fullName: 'Amy', department: 'IT'),
            needFood: true,
          ),
          FoodEmployee(
            person: PersonSummary(id: '2', employeeId: 'E2', fullName: 'Bob', department: 'HR'),
            needFood: true,
          ),
        ],
      );
    await tester.pumpWidget(_wrap(const FoodReportScreen(), repo));
    await tester.pumpAndSettle();
    expect(find.text('HR (1)'), findsOneWidget);
    expect(find.text('IT (1)'), findsOneWidget);
    expect(find.text('Amy'), findsOneWidget);
    expect(find.text('Not answered'), findsOneWidget);
  });
}
