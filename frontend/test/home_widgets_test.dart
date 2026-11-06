import 'dart:async';

import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/files/file_service.dart';
import 'package:employee_connect/core/widgets/skeleton.dart';
import 'package:employee_connect/features/announcements/data/announcement_repository.dart';
import 'package:employee_connect/features/announcements/domain/announcement_models.dart';
import 'package:employee_connect/features/attendance/data/attendance_repository.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart';
import 'package:employee_connect/features/auth/presentation/auth_controller.dart';
import 'package:employee_connect/features/food/data/food_repository.dart';
import 'package:employee_connect/features/food/domain/food_models.dart';
import 'package:employee_connect/features/home/data/home_repository.dart';
import 'package:employee_connect/features/home/domain/home_models.dart';
import 'package:employee_connect/features/home/presentation/home_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/attendance_fakes.dart';
import 'helpers/food_fakes.dart';
import 'helpers/module_fakes.dart';

final _now = DateTime(2026, 10, 21, 10);

class _OfflineAttendance extends FakeAttendanceRepo {
  @override
  Future<TodayAttendance> today() async =>
      throw const ApiException(code: ApiException.networkCode, message: 'No connection.');
}

class _Rig {
  _Rig({HomeData? data})
      : home = FakeHomeRepo(data ?? sampleHome(now: _now)),
        announcements = FakeAnnouncementRepo([
          ann0('a1', 'Fire drill', AnnouncementPriority.urgent),
          ann0('a2', 'Menu update', AnnouncementPriority.normal, read: true),
        ])
          ..unread = 3,
        food = FakeFoodRepo([const FoodDay(date: '2026-10-21')]),
        attendance = FakeAttendanceRepo();

  final FakeHomeRepo home;
  final FakeAnnouncementRepo announcements;
  final FakeFoodRepo food;
  FakeAttendanceRepo attendance;

  static Announcement ann0(String id, String title, AnnouncementPriority p, {bool read = false}) =>
      ann(id, title: title, priority: p, isRead: read);

  List<Override> get overrides => [
        homeRepositoryProvider.overrideWithValue(home),
        announcementRepositoryProvider.overrideWithValue(announcements),
        foodRepositoryProvider.overrideWithValue(food),
        attendanceRepositoryProvider.overrideWithValue(attendance),
        authControllerProvider.overrideWith(() => FakeAuth(userWith(const []))),
        fileServiceProvider.overrideWithValue(FakeFileService()),
      ];

  Widget widget({ThemeMode mode = ThemeMode.light}) =>
      app(HomeScreen(clock: () => _now), overrides, mode: mode);
}

void _tall(WidgetTester t) {
  t.view.physicalSize = const Size(800, 3200);
  t.view.devicePixelRatio = 1;
  addTearDown(t.view.reset);
}

Switch _foodSwitch(WidgetTester t) => t.widget<Switch>(find.byKey(const ValueKey('home-food-switch')));

void main() {
  testWidgets('shows every dashboard section', (tester) async {
    _tall(tester);
    final rig = _Rig();
    await tester.pumpWidget(rig.widget());
    await tester.pumpAndSettle();

    expect(find.text('Good morning, Asha'), findsOneWidget);
    // Today status card (existing widget)
    expect(find.text('Not checked in yet'), findsOneWidget);
    // Quick actions
    for (final label in ['Attendance', 'Apply leave', 'Food', 'Announcements', 'Profile']) {
      expect(find.widgetWithText(ActionChip, label), findsOneWidget, reason: label);
    }
    // Food today
    expect(find.text('Need food today?'), findsOneWidget);
    expect(_foodSwitch(tester).onChanged, isNotNull);
    // Balances
    expect(find.text('Casual: 8'), findsOneWidget);
    expect(find.text('Sick: 7.5'), findsOneWidget);
    expect(find.text('Permission this month: 2 left, 3h'), findsOneWidget);
    expect(find.text('1 request waiting for approval'), findsOneWidget);
    // Announcements preview
    expect(find.text('Fire drill'), findsOneWidget);
    expect(find.text('Menu update'), findsOneWidget);
    expect(find.text('View all (3 unread)'), findsOneWidget);
    // Celebrations
    expect(find.text('Meera Nair'), findsOneWidget);
    expect(find.text('Birthday - Today'), findsOneWidget);
    expect(find.text('Anniversary - Fri 23 Oct'), findsOneWidget);
  });

  testWidgets('shows a skeleton while loading, then the data', (tester) async {
    _tall(tester);
    final gate = Completer<void>();
    final rig = _Rig()..home.gate = gate.future;
    await tester.pumpWidget(rig.widget(mode: ThemeMode.dark));
    await tester.pump();
    expect(find.byType(SkeletonCard), findsWidgets);
    expect(find.text('Casual: 8'), findsNothing);

    gate.complete();
    await tester.pumpAndSettle();
    expect(find.byType(SkeletonCard), findsNothing);
    expect(find.text('Casual: 8'), findsOneWidget);
  });

  testWidgets('parts the server could not compute are left out, the rest still shows', (tester) async {
    _tall(tester);
    final rig = _Rig(data: HomeData.fromJson({'unread_announcements': 2, 'celebrations': <Object>[]}));
    await tester.pumpWidget(rig.widget());
    await tester.pumpAndSettle();
    expect(find.text('Need food today?'), findsNothing);
    expect(find.text('Balances are not available right now.'), findsOneWidget);
    expect(find.textContaining('Nothing to celebrate'), findsOneWidget);
    expect(find.text('Fire drill'), findsOneWidget);
  });

  testWidgets('error state keeps quick actions and can retry', (tester) async {
    _tall(tester);
    final rig = _Rig();
    rig.home.error = const ApiException(code: 'INTERNAL_ERROR', message: 'Server trouble.', status: 500);
    await tester.pumpWidget(rig.widget());
    await tester.pumpAndSettle();
    expect(find.text('Server trouble.'), findsOneWidget);
    expect(find.widgetWithText(ActionChip, 'Attendance'), findsOneWidget);

    rig.home.error = null;
    await tester.tap(find.text('Try again'));
    await tester.pumpAndSettle();
    expect(find.text('Casual: 8'), findsOneWidget);
  });

  testWidgets('offline: cached dashboard, banner, food switch disabled, attendance falls back', (tester) async {
    _tall(tester);
    final rig = _Rig()
      ..home.stale = true
      ..attendance = _OfflineAttendance();
    await tester.pumpWidget(rig.widget());
    await tester.pumpAndSettle();
    expect(find.text('Offline. Showing saved data.'), findsOneWidget);
    expect(_foodSwitch(tester).onChanged, isNull);
    expect(find.textContaining('Offline - connect to change'), findsOneWidget);
    expect(find.textContaining('Checked in at'), findsOneWidget);
  });

  group('food quick toggle', () {
    testWidgets('saves for today through the food endpoint', (tester) async {
      _tall(tester);
      final rig = _Rig();
      await tester.pumpWidget(rig.widget());
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('home-food-switch')));
      await tester.pump();
      expect(_foodSwitch(tester).value, isTrue); // optimistic
      await tester.pumpAndSettle();
      expect(rig.food.sets, [('2026-10-21', true)]);
      expect(_foodSwitch(tester).value, isTrue);
      expect(find.text('Answer: Yes'), findsOneWidget);
    });

    testWidgets('cut-off error rolls back, locks the switch and explains', (tester) async {
      _tall(tester);
      final rig = _Rig();
      rig.food.setError = const ApiException(code: 'FOOD_CUTOFF_PASSED', message: 'x', status: 409);
      await tester.pumpWidget(rig.widget());
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('home-food-switch')));
      await tester.pumpAndSettle();
      expect(_foodSwitch(tester).value, isFalse);
      expect(_foodSwitch(tester).onChanged, isNull);
      expect(find.textContaining('cut-off time has passed'), findsOneWidget);
    });

    testWidgets('disabled when the server says it cannot be changed', (tester) async {
      _tall(tester);
      final rig = _Rig(data: sampleHome(now: _now, canChangeFood: false));
      await tester.pumpWidget(rig.widget());
      await tester.pumpAndSettle();
      expect(_foodSwitch(tester).onChanged, isNull);
      expect(find.text('Cut-off has passed for today.'), findsOneWidget);
      expect(rig.food.sets, isEmpty);
    });
  });

  testWidgets('copying a wish puts the message on the clipboard', (tester) async {
    _tall(tester);
    String? copied;
    tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(SystemChannels.platform, (call) async {
      if (call.method == 'Clipboard.setData') {
        copied = (call.arguments as Map)['text'] as String?;
      }
      return null;
    });
    addTearDown(
      () => tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(SystemChannels.platform, null),
    );
    final rig = _Rig();
    await tester.pumpWidget(rig.widget());
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('Copy a wish for Meera Nair'));
    await tester.pumpAndSettle();
    expect(copied, startsWith('Happy birthday, Meera!'));
    expect(find.textContaining('Wish copied'), findsOneWidget);
  });

  testWidgets('tapping an announcement opens it and marks it read', (tester) async {
    _tall(tester);
    final rig = _Rig();
    await tester.pumpWidget(rig.widget());
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('home-ann-a1')));
    await tester.pumpAndSettle();
    expect(rig.announcements.readIds, ['a1']);
  });
}
