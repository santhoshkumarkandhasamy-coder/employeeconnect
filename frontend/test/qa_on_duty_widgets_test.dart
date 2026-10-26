// QA: On Duty widget behaviour against the contract (uses the shared fakes only, not the implementer's test file).
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/attendance/data/attendance_repository.dart';
import 'package:employee_connect/features/attendance/data/location_service.dart';
import 'package:employee_connect/features/attendance/data/selfie_service.dart';
import 'package:employee_connect/features/attendance/presentation/attendance_screen.dart';
import 'package:employee_connect/features/on_duty/data/maps_launcher.dart';
import 'package:employee_connect/features/on_duty/data/on_duty_repository.dart';
import 'package:employee_connect/features/on_duty/domain/on_duty_config.dart';
import 'package:employee_connect/features/on_duty/domain/on_duty_models.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/attendance_fakes.dart';
import 'helpers/on_duty_fakes.dart';

Widget _wrap(Widget child, List<Override> overrides) => ProviderScope(
      key: UniqueKey(),
      overrides: overrides,
      child: MaterialApp(theme: ThemeData(useMaterial3: true), home: child),
    );

List<Override> _ov(FakeAttendanceRepo att, FakeOnDutyRepo duty, {FakeLocation? loc}) => [
      attendanceRepositoryProvider.overrideWithValue(att),
      onDutyRepositoryProvider.overrideWithValue(duty),
      locationServiceProvider.overrideWithValue(loc ?? FakeLocation()),
      selfieServiceProvider.overrideWithValue(FakeSelfie()),
      mapsLauncherProvider.overrideWithValue(FakeMaps()),
    ];

void _tall(WidgetTester t) {
  t.view.physicalSize = const Size(800, 2600);
  t.view.devicePixelRatio = 1;
  addTearDown(t.view.reset);
}

Future<void> _openOnDuty(WidgetTester t) async {
  await t.tap(find.text('On Duty'));
  await t.pumpAndSettle();
}

bool _checkInEnabled(WidgetTester t) {
  final btn = t.widget<FilledButton>(find.ancestor(of: find.text('Check in here'), matching: find.byType(FilledButton)));
  return btn.onPressed != null;
}

void main() {
  testWidgets('every blocking reason disables check-in and explains itself', (tester) async {
    _tall(tester);
    for (final reason in ['ON_DUTY_DISABLED', 'NO_SHIFT_ASSIGNED', 'ON_LEAVE', 'VISIT_OPEN', 'MAX_VISITS_REACHED']) {
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      final duty = FakeOnDutyRepo()..todayData = OnDutyToday(blockedReason: reason);
      await tester.pumpWidget(_wrap(const AttendanceScreen(), _ov(att, duty, loc: FakeLocation(fix: LocationFix(lat: acme.lat, lng: acme.lng, accuracyM: 8, timestamp: DateTime.now())))));
      await tester.pumpAndSettle();
      await _openOnDuty(tester);
      final text = blockedReasonText(reason)!;
      expect(find.textContaining(text), findsWidgets, reason: reason);
      if (find.text('Check in here').evaluate().isNotEmpty) {
        expect(_checkInEnabled(tester), isFalse, reason: reason);
      }
      // tapping must never send a punch
      expect(duty.checkIns, isEmpty, reason: reason);
    }
  });

  testWidgets('mode switch hidden when the config block is missing', (tester) async {
    _tall(tester);
    final att = FakeAttendanceRepo()..cfg = configWith(OnDutyConfig.none);
    await tester.pumpWidget(_wrap(const AttendanceScreen(), _ov(att, FakeOnDutyRepo())));
    await tester.pumpAndSettle();
    expect(find.text('On Duty'), findsNothing);
    expect(find.text('Check in'), findsOneWidget);
  });

  testWidgets('RATE_LIMITED and low accuracy show a message, keep the form usable and do not claim success', (tester) async {
    _tall(tester);
    for (final code in ['RATE_LIMITED', 'LOCATION_ACCURACY_LOW', 'LOCATION_FIX_STALE']) {
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      final duty = FakeOnDutyRepo()..checkInError = ApiException(code: code, message: 'srv', status: 422);
      await tester.pumpWidget(_wrap(const AttendanceScreen(), _ov(att, duty, loc: FakeLocation(fix: LocationFix(lat: acme.lat, lng: acme.lng, accuracyM: 8, timestamp: DateTime.now())))));
      await tester.pumpAndSettle();
      await _openOnDuty(tester);
      await tester.tap(find.text('Check in here'));
      await tester.pumpAndSettle();
      expect(duty.checkIns.length, 1, reason: code);
      expect(find.textContaining('On duty at'), findsNothing, reason: code);
      expect(find.text('Check in here'), findsOneWidget, reason: code);
    }
  });

  testWidgets('no open visit means no Check out button; open visit hides Check in here', (tester) async {
    _tall(tester);
    final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
    final duty = FakeOnDutyRepo();
    await tester.pumpWidget(_wrap(const AttendanceScreen(), _ov(att, duty)));
    await tester.pumpAndSettle();
    await _openOnDuty(tester);
    expect(find.text('Check out'), findsNothing);
    final v = duty.visit('v1');
    duty.todayData = OnDutyToday(openVisit: v, visits: [v]);
    await tester.pumpWidget(_wrap(const AttendanceScreen(), _ov(att, duty)));
    await tester.pumpAndSettle();
    await _openOnDuty(tester);
    expect(find.text('Check in here'), findsNothing);
    expect(find.text('Check out'), findsOneWidget);
  });
}
