import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/cache/json_cache.dart';
import 'package:employee_connect/features/attendance/data/attendance_repository.dart';
import 'package:employee_connect/features/attendance/data/location_service.dart';
import 'package:employee_connect/features/attendance/data/selfie_service.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart';
import 'package:employee_connect/features/attendance/presentation/attendance_screen.dart';
import 'package:employee_connect/features/attendance/presentation/history_tab.dart';
import 'package:employee_connect/features/attendance/presentation/widgets/summary_card.dart';
import 'package:employee_connect/features/leave/data/leave_repository.dart';
import 'package:employee_connect/features/leave/domain/leave_models.dart';
import 'package:employee_connect/features/leave/presentation/approvals_screen.dart';
import 'package:employee_connect/features/leave/presentation/leave_screen.dart';
import 'package:employee_connect/features/leave/presentation/widgets/form_widgets.dart';
import 'package:employee_connect/features/leave/presentation/widgets/request_widgets.dart';
import 'package:employee_connect/features/on_duty/data/maps_launcher.dart';
import 'package:employee_connect/features/on_duty/data/on_duty_repository.dart';
import 'package:employee_connect/features/on_duty/domain/on_duty_config.dart';
import 'package:employee_connect/features/on_duty/domain/on_duty_models.dart';
import 'package:employee_connect/features/on_duty/presentation/on_duty_request_form.dart';
import 'package:employee_connect/features/auth/presentation/auth_controller.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/attendance_fakes.dart';
import 'helpers/module_fakes.dart' show FakeAuth, userWith;
import 'helpers/on_duty_fakes.dart';

Widget wrap(Widget child, List<Override> overrides, {ThemeMode mode = ThemeMode.light}) => ProviderScope(
      key: UniqueKey(),
      overrides: overrides,
      child: MaterialApp(
        themeMode: mode,
        theme: ThemeData(useMaterial3: true),
        darkTheme: ThemeData(useMaterial3: true, brightness: Brightness.dark),
        home: child,
      ),
    );

List<Override> overridesFor({
  required FakeAttendanceRepo att,
  required FakeOnDutyRepo duty,
  FakeLocation? loc,
  FakeSelfie? selfie,
  FakeMaps? maps,
}) =>
    [
      attendanceRepositoryProvider.overrideWithValue(att),
      onDutyRepositoryProvider.overrideWithValue(duty),
      locationServiceProvider.overrideWithValue(loc ?? FakeLocation()),
      selfieServiceProvider.overrideWithValue(selfie ?? FakeSelfie()),
      mapsLauncherProvider.overrideWithValue(maps ?? FakeMaps()),
    ];

void tall(WidgetTester t) {
  t.view.physicalSize = const Size(800, 2600);
  t.view.devicePixelRatio = 1;
  addTearDown(t.view.reset);
}

LocationFix atAcme() =>
    LocationFix(lat: acme.lat, lng: acme.lng, accuracyM: 8, timestamp: DateTime.now());

Future<void> openOnDuty(WidgetTester tester) async {
  await tester.tap(find.text('On Duty'));
  await tester.pumpAndSettle();
}

void main() {
  group('Today tab mode switch', () {
    testWidgets('hidden when On Duty is disabled or the employee has no site', (tester) async {
      tall(tester);
      for (final cfg in const [OnDutyConfig.none, OnDutyConfig(enabled: true), OnDutyConfig(hasLocations: true)]) {
        final att = FakeAttendanceRepo()..cfg = configWith(cfg);
        await tester.pumpWidget(wrap(const AttendanceScreen(), overridesFor(att: att, duty: FakeOnDutyRepo())));
        await tester.pumpAndSettle();
        expect(find.text('Office'), findsNothing, reason: '$cfg');
        expect(find.text('On Duty'), findsNothing);
        expect(find.text('Check in'), findsOneWidget);
      }
    });

    testWidgets('shown when enabled and a site is assigned; office stays the default', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      await tester.pumpWidget(wrap(const AttendanceScreen(), overridesFor(att: att, duty: FakeOnDutyRepo())));
      await tester.pumpAndSettle();
      expect(find.text('Office'), findsOneWidget);
      expect(find.text('On Duty'), findsOneWidget);
      expect(find.text('Check in'), findsOneWidget);
      expect(find.text('Check in here'), findsNothing);
      await openOnDuty(tester);
      expect(find.text('Check in here'), findsOneWidget);
      expect(find.text('Check in'), findsNothing);
      await tester.tap(find.text('Office'));
      await tester.pumpAndSettle();
      expect(find.text('Check in'), findsOneWidget);
    });
  });

  group('On Duty panel', () {
    testWidgets('lists sites nearest first with a nearby badge and an Open in Maps button', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      final duty = FakeOnDutyRepo()..sites = [branch, acme];
      final maps = FakeMaps();
      await tester.pumpWidget(
        wrap(const AttendanceScreen(), overridesFor(att: att, duty: duty, loc: FakeLocation(fix: atAcme()), maps: maps)),
      );
      await tester.pumpAndSettle();
      await openOnDuty(tester);
      final acmeY = tester.getTopLeft(find.text(acme.name)).dy;
      final branchY = tester.getTopLeft(find.text(branch.name)).dy;
      expect(acmeY, lessThan(branchY));
      expect(find.text('Nearby'), findsOneWidget);
      await tester.tap(find.byTooltip('Open ${acme.name} in Maps'));
      await tester.pumpAndSettle();
      expect(maps.opened.single.id, 'l1');
    });

    testWidgets('check in sends the chosen site and one fix, then shows the open visit', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      final duty = FakeOnDutyRepo();
      final loc = CountingLocation(fix: atAcme());
      await tester.pumpWidget(wrap(const AttendanceScreen(), overridesFor(att: att, duty: duty, loc: loc)));
      await tester.pumpAndSettle();
      await openOnDuty(tester);
      final readsBefore = loc.reads;
      await tester.enterText(find.widgetWithText(TextField, 'Note (optional)'), '  Quarterly review ');
      await tester.tap(find.text('Check in here'));
      await tester.pumpAndSettle();

      expect(duty.checkIns.single.locationId, 'l1');
      expect(duty.checkIns.single.note, '  Quarterly review ');
      expect(duty.checkIns.single.fix.lat, acme.lat);
      // one-shot: a single read for the punch itself, and nothing keeps reading
      expect(loc.reads - readsBefore, 1);
      final after = loc.reads;
      await tester.pump(const Duration(minutes: 5));
      expect(loc.reads, after, reason: 'no periodic or streamed location reads');
      expect(find.textContaining('On duty at ${acme.name} since 10:12'), findsOneWidget);
      expect(find.text('Check out'), findsOneWidget);
      expect(find.textContaining('Checked in at ${acme.name}'), findsOneWidget);
    });

    testWidgets('choosing another site checks in there', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      final duty = FakeOnDutyRepo();
      await tester.pumpWidget(
        wrap(const AttendanceScreen(), overridesFor(att: att, duty: duty, loc: FakeLocation(fix: atAcme()))),
      );
      await tester.pumpAndSettle();
      await openOnDuty(tester);
      await tester.tap(find.text(branch.name));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Check in here'));
      await tester.pumpAndSettle();
      expect(duty.checkIns.single.locationId, 'l2');
    });

    testWidgets('OUTSIDE_DUTY_LOCATION names the site and shows no distance', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      final duty = FakeOnDutyRepo()
        ..checkInError = const ApiException(
          code: 'OUTSIDE_DUTY_LOCATION',
          message: 'Outside',
          status: 422,
          details: [FieldError('within_fence', 'false')],
        );
      await tester.pumpWidget(
        wrap(const AttendanceScreen(), overridesFor(att: att, duty: duty, loc: FakeLocation(fix: atAcme()))),
      );
      await tester.pumpAndSettle();
      await openOnDuty(tester);
      await tester.tap(find.text('Check in here'));
      await tester.pumpAndSettle();
      expect(find.textContaining('You are outside ${acme.name}'), findsOneWidget);
      expect(find.textContaining('false'), findsNothing);
      // no panel state was lost: the button is usable again
      expect(find.text('Check in here'), findsOneWidget);
    });

    testWidgets('each blocking error is mapped and the state is reloaded', (tester) async {
      tall(tester);
      final cases = {
        'MAX_VISITS_REACHED': 'visit limit',
        'ON_DUTY_NOT_APPROVED': 'Request On Duty',
        'DUTY_LOCATION_UNAVAILABLE': 'not available to you',
        'ON_DUTY_DISABLED': 'turned off',
        'VISIT_OPEN': 'open visit',
        'MOCK_LOCATION_DETECTED': 'mock',
      };
      for (final e in cases.entries) {
        final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
        final duty = FakeOnDutyRepo()..checkInError = ApiException(code: e.key, message: 'srv', status: 409);
        await tester.pumpWidget(
          wrap(const AttendanceScreen(), overridesFor(att: att, duty: duty, loc: FakeLocation(fix: atAcme()))),
        );
        await tester.pumpAndSettle();
        await openOnDuty(tester);
        final before = duty.todayCalls;
        await tester.tap(find.text('Check in here'));
        await tester.pumpAndSettle();
        expect(find.textContaining(e.value), findsWidgets, reason: e.key);
        expect(find.text('srv'), findsNothing, reason: e.key);
        if (e.key != 'MOCK_LOCATION_DETECTED') {
          expect(duty.todayCalls, greaterThan(before), reason: '${e.key} reloads /on-duty/today');
        }
      }
    });

    testWidgets('approval needed: banner with Request On Duty, check-in disabled', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      final duty = FakeOnDutyRepo()
        ..todayData = const OnDutyToday(
          blockedReason: 'ON_DUTY_NOT_APPROVED',
          approval: OnDutyApproval(required: true),
        );
      await tester.pumpWidget(wrap(const AttendanceScreen(), overridesFor(att: att, duty: duty)));
      await tester.pumpAndSettle();
      await openOnDuty(tester);
      expect(find.textContaining('Approval needed'), findsOneWidget);
      expect(find.text('Request On Duty'), findsOneWidget);
      final btn = tester.widget<FilledButton>(find.ancestor(
        of: find.text('Check in here'),
        matching: find.byType(FilledButton),
      ),);
      expect(btn.onPressed, isNull);
      await tester.tap(find.text('Request On Duty'));
      await tester.pumpAndSettle();
      expect(find.byType(OnDutyRequestForm), findsOneWidget);
    });

    testWidgets('approved for another site only: this site is not selectable for check-in', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      final duty = FakeOnDutyRepo()
        ..todayData = const OnDutyToday(
          canCheckIn: true,
          approval: OnDutyApproval(required: true, covered: true, locationIds: ['l2']),
        );
      await tester.pumpWidget(
        wrap(const AttendanceScreen(), overridesFor(att: att, duty: duty, loc: FakeLocation(fix: atAcme()))),
      );
      await tester.pumpAndSettle();
      await openOnDuty(tester);
      // nearest site (Acme) is not covered
      expect(find.textContaining('does not cover ${acme.name}'), findsOneWidget);
      var btn = tester.widget<FilledButton>(find.ancestor(
        of: find.text('Check in here'),
        matching: find.byType(FilledButton),
      ),);
      expect(btn.onPressed, isNull);
      await tester.tap(find.text(branch.name));
      await tester.pumpAndSettle();
      btn = tester.widget<FilledButton>(find.ancestor(
        of: find.text('Check in here'),
        matching: find.byType(FilledButton),
      ),);
      expect(btn.onPressed, isNotNull);
    });

    testWidgets('other blocked reasons explain themselves', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      final duty = FakeOnDutyRepo()..todayData = const OnDutyToday(blockedReason: 'ON_LEAVE');
      await tester.pumpWidget(wrap(const AttendanceScreen(), overridesFor(att: att, duty: duty)));
      await tester.pumpAndSettle();
      await openOnDuty(tester);
      expect(find.textContaining('approved leave'), findsOneWidget);
      expect(find.text('Request On Duty'), findsNothing);
    });

    testWidgets('open visit: check out sends the visit id and the note', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      final duty = FakeOnDutyRepo();
      final v = duty.visit('v9');
      duty.todayData = OnDutyToday(openVisit: v, visits: [v], visitsRemaining: 9);
      await tester.pumpWidget(
        wrap(const AttendanceScreen(), overridesFor(att: att, duty: duty, loc: FakeLocation(fix: atAcme()))),
      );
      await tester.pumpAndSettle();
      await openOnDuty(tester);
      expect(find.textContaining('On duty at ${acme.name} since 10:12'), findsOneWidget);
      expect(find.text('Check in here'), findsNothing);
      await tester.enterText(find.widgetWithText(TextField, 'Note for this visit (optional)'), 'Done');
      await tester.tap(find.text('Check out'));
      await tester.pumpAndSettle();
      expect(duty.checkOuts.single.visitId, 'v9');
      expect(duty.checkOuts.single.note, 'Done');
      expect(find.textContaining('Checked out of ${acme.name}'), findsOneWidget);
      // timeline shows the closed visit with its duration
      expect(find.textContaining('(1h 35m)'), findsWidgets);
    });

    testWidgets('NO_OPEN_VISIT on check-out reloads the state', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      final duty = FakeOnDutyRepo();
      final v = duty.visit('v9');
      duty.todayData = OnDutyToday(openVisit: v, visits: [v]);
      duty.checkOutError = const ApiException(code: 'NO_OPEN_VISIT', message: 'x', status: 409);
      await tester.pumpWidget(
        wrap(const AttendanceScreen(), overridesFor(att: att, duty: duty, loc: FakeLocation(fix: atAcme()))),
      );
      await tester.pumpAndSettle();
      await openOnDuty(tester);
      final before = duty.todayCalls;
      duty.todayData = const OnDutyToday(canCheckIn: true);
      await tester.tap(find.text('Check out'));
      await tester.pumpAndSettle();
      expect(find.textContaining('no open visit'), findsOneWidget);
      expect(duty.todayCalls, greaterThan(before));
      expect(find.text('Check in here'), findsOneWidget);
    });

    testWidgets('location permission: rationale first; declining sends nothing', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      final duty = FakeOnDutyRepo();
      final loc = CountingLocation(state: LocationAccess.denied, fix: atAcme());
      await tester.pumpWidget(wrap(const AttendanceScreen(), overridesFor(att: att, duty: duty, loc: loc)));
      await tester.pumpAndSettle();
      await openOnDuty(tester);
      await tester.tap(find.text('Check in here'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      expect(find.text('Allow location access?'), findsOneWidget);
      expect(find.textContaining('not tracked in the background'), findsOneWidget);
      await tester.tap(find.text('Not now'));
      await tester.pumpAndSettle();
      expect(loc.requests, 0);
      expect(duty.checkIns, isEmpty);
      expect(find.textContaining('Location permission is needed'), findsOneWidget);
    });

    testWidgets('permission blocked forever offers Settings', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      final duty = FakeOnDutyRepo();
      final loc = FakeLocation(state: LocationAccess.deniedForever);
      await tester.pumpWidget(wrap(const AttendanceScreen(), overridesFor(att: att, duty: duty, loc: loc)));
      await tester.pumpAndSettle();
      await openOnDuty(tester);
      await tester.tap(find.text('Check in here'));
      await tester.pumpAndSettle();
      expect(find.text('Open settings'), findsWidgets);
      await tester.tap(find.text('Open settings').last);
      expect(loc.settingsOpened, 1);
      expect(duty.checkIns, isEmpty);
    });

    testWidgets('an imprecise fix is not sent', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      final duty = FakeOnDutyRepo();
      final loc = FakeLocation(fix: LocationFix(lat: 1, lng: 1, accuracyM: 900, timestamp: DateTime.now()));
      await tester.pumpWidget(wrap(const AttendanceScreen(), overridesFor(att: att, duty: duty, loc: loc)));
      await tester.pumpAndSettle();
      await openOnDuty(tester);
      await tester.tap(find.text('Check in here'));
      await tester.pumpAndSettle();
      expect(duty.checkIns, isEmpty);
      expect(find.textContaining('not accurate enough'), findsOneWidget);
    });

    testWidgets('selfie required: captures, uploads and sends the file id', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()..cfg = configWith(const OnDutyConfig(enabled: true, hasLocations: true, selfieRequired: true));
      final duty = FakeOnDutyRepo();
      final selfie = FakeSelfie('/tmp/s.jpg');
      await tester.pumpWidget(
        wrap(const AttendanceScreen(), overridesFor(att: att, duty: duty, loc: FakeLocation(fix: atAcme()), selfie: selfie)),
      );
      await tester.pumpAndSettle();
      await openOnDuty(tester);
      expect(find.text('Selfie required'), findsOneWidget);
      await tester.tap(find.text('Check in here'));
      await tester.pumpAndSettle();
      expect(selfie.captures, 1);
      expect(duty.checkIns.single.selfie, 'file-1');
      expect(selfie.discarded, contains('/tmp/s.jpg'));
    });

    testWidgets('empty, loading-error and retry states', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      final duty = FakeOnDutyRepo()..sites = [];
      await tester.pumpWidget(wrap(const AttendanceScreen(), overridesFor(att: att, duty: duty)));
      await tester.pumpAndSettle();
      await openOnDuty(tester);
      expect(find.text('No on-duty locations are assigned to you.'), findsOneWidget);

      final bad = FakeOnDutyRepo()..todayError = const ApiException(code: ApiException.networkCode, message: 'No connection.');
      await tester.pumpWidget(wrap(const AttendanceScreen(), overridesFor(att: att, duty: bad)));
      await tester.pumpAndSettle();
      await openOnDuty(tester);
      expect(find.text('No connection.'), findsOneWidget);
      bad.todayError = null;
      await tester.tap(find.text('Try again'));
      await tester.pumpAndSettle();
      expect(find.text('Check in here'), findsOneWidget);
    });

    testWidgets('renders on a small phone in dark mode', (tester) async {
      tester.view.physicalSize = const Size(320, 640);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      final duty = FakeOnDutyRepo();
      await tester.pumpWidget(
        wrap(const AttendanceScreen(), overridesFor(att: att, duty: duty, loc: FakeLocation(fix: atAcme())), mode: ThemeMode.dark),
      );
      await tester.pumpAndSettle();
      await openOnDuty(tester);
      expect(tester.takeException(), isNull);
      expect(find.text(acme.name), findsOneWidget);
    });
  });

  group('office End day with visits', () {
    AttendanceRecord started({String? source, String mode = 'mixed'}) => AttendanceRecord(
          id: 'r1',
          workDate: '2026-10-26',
          status: DayStatus.present,
          checkInAt: DateTime(2026, 10, 26, 9, 5),
          checkOutAt: source == null ? null : DateTime(2026, 10, 26, 15),
          workMode: mode,
          checkOutSource: source,
        );

    testWidgets('a day with visits says End day; blocked while a visit is open', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()
        ..cfg = configWith(onDutyOn)
        ..todayRecord = started();
      final duty = FakeOnDutyRepo();
      final v = duty.visit('v1');
      duty.todayData = OnDutyToday(openVisit: v, visits: [v]);
      await tester.pumpWidget(wrap(const AttendanceScreen(), overridesFor(att: att, duty: duty)));
      await tester.pumpAndSettle();
      expect(find.text('End day'), findsOneWidget);
      final btn = tester.widget<FilledButton>(find.ancestor(of: find.text('End day'), matching: find.byType(FilledButton)));
      expect(btn.onPressed, isNull);
      expect(find.textContaining('Check out of your On Duty visit first'), findsOneWidget);
    });

    testWidgets('End day works when no visit is open and calls the office check-out', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()
        ..cfg = configWith(onDutyOn)
        ..todayRecord = started();
      final duty = FakeOnDutyRepo();
      await tester.pumpWidget(
        wrap(const AttendanceScreen(), overridesFor(att: att, duty: duty, loc: FakeLocation(fix: atAcme()))),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('End day'));
      await tester.pumpAndSettle();
      expect(att.checkOuts, 1);
    });

    testWidgets('a check-out set by a visit does not mean the office day is done', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()
        ..cfg = configWith(onDutyOn)
        ..todayRecord = started(source: 'on_duty', mode: 'on_duty');
      await tester.pumpWidget(wrap(const AttendanceScreen(), overridesFor(att: att, duty: FakeOnDutyRepo())));
      await tester.pumpAndSettle();
      expect(find.text('Done for today'), findsNothing);
      expect(find.text('End day'), findsOneWidget);
    });

    testWidgets('an office check-out ends the day', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()
        ..cfg = configWith(onDutyOn)
        ..todayRecord = started(source: 'office');
      await tester.pumpWidget(wrap(const AttendanceScreen(), overridesFor(att: att, duty: FakeOnDutyRepo())));
      await tester.pumpAndSettle();
      expect(find.text('Done for today'), findsOneWidget);
    });

    testWidgets('VISIT_OPEN from the server shows the explanation', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()
        ..cfg = configWith(onDutyOn)
        ..todayRecord = started();
      // The server may know about a visit the app has not loaded yet.
      final duty = FakeOnDutyRepo();
      final checkOutFails = _VisitOpenAttendanceRepo(att);
      await tester.pumpWidget(
        wrap(const AttendanceScreen(), [
          ...overridesFor(att: checkOutFails, duty: duty, loc: FakeLocation(fix: atAcme())),
        ]),
      );
      await tester.pumpAndSettle();
      final before = duty.todayCalls;
      await tester.tap(find.text('End day'));
      await tester.pumpAndSettle();
      expect(find.textContaining('still on an on-duty visit'), findsOneWidget);
      expect(duty.todayCalls, greaterThan(before));
    });
  });

  group('history, calendar and summary', () {
    testWidgets('history tile for a day that started on duty (null within_fence)', (tester) async {
      tall(tester);
      final record = AttendanceRecord.fromJson({
        'id': 'a',
        'work_date': '2026-10-20',
        'status': 'present',
        'check_in_at': '2026-10-20T04:30:00Z',
        'check_out_at': '2026-10-20T12:00:00Z',
        'worked_minutes': 450,
        'within_fence': null,
        'distance_m': null,
        'work_mode': 'on_duty',
        'check_out_source': 'on_duty',
        'on_duty_locations': ['Acme', 'Branch'],
      });
      await tester.pumpWidget(wrap(Scaffold(body: HistoryTile(record: record)), const []));
      expect(find.text('On Duty'), findsOneWidget);
      expect(find.textContaining('On Duty: Acme, Branch'), findsOneWidget);
      expect(find.text('Present'), findsOneWidget);
    });

    testWidgets('mixed day label', (tester) async {
      expect(
        onDutyDayLabelFor('mixed', ['Acme']),
        'Office + On Duty: Acme',
      );
    });

    testWidgets('calendar day with On Duty opens the visits list', (tester) async {
      tall(tester);
      final att = FakeAttendanceRepo()
        ..cfg = configWith(onDutyOn)
        ..calendarDays = [
          CalendarDay(date: DateTime.now(), status: DayStatus.present, onDuty: true),
        ];
      final duty = FakeOnDutyRepo();
      await tester.pumpWidget(wrap(const AttendanceScreen(), overridesFor(att: att, duty: duty)));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Calendar'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('${DateTime.now().day}').first);
      await tester.pumpAndSettle();
      expect(find.widgetWithText(Chip, 'On Duty'), findsOneWidget);
    });

    testWidgets('summary card mentions On Duty days only when there are some', (tester) async {
      await tester.pumpWidget(
        wrap(
          const Scaffold(
            body: AttendanceSummaryCard(summary: AttendanceSummary(month: '2026-10', present: 5, onDutyDays: 3)),
          ),
          const [],
        ),
      );
      expect(find.textContaining('On Duty days: 3'), findsOneWidget);
      await tester.pumpWidget(
        wrap(
          const Scaffold(body: AttendanceSummaryCard(summary: AttendanceSummary(month: '2026-10'))),
          const [],
        ),
      );
      expect(find.textContaining('On Duty days'), findsNothing);
    });
  });

  group('On Duty requests', () {
    List<Override> leaveOverrides(FakeAttendanceRepo att, FakeOnDutyRepo duty, _Leave leave) => [
          ...overridesFor(att: att, duty: duty),
          leaveRepositoryProvider.overrideWithValue(leave),
        ];

    testWidgets('the Request tab shows the On duty segment only with sites', (tester) async {
      tall(tester);
      final off = FakeAttendanceRepo()..cfg = configWith(OnDutyConfig.none);
      await tester.pumpWidget(wrap(const LeaveScreen(), leaveOverrides(off, FakeOnDutyRepo(), _Leave())));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Request'));
      await tester.pumpAndSettle();
      expect(find.text('Permission'), findsWidgets);
      expect(find.text('On duty'), findsNothing);

      final on = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      await tester.pumpWidget(wrap(const LeaveScreen(), leaveOverrides(on, FakeOnDutyRepo(), _Leave())));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Request'));
      await tester.pumpAndSettle();
      expect(find.text('On duty'), findsOneWidget);
    });

    testWidgets('form: purpose and dates are validated before anything is sent', (tester) async {
      tall(tester);
      final duty = FakeOnDutyRepo();
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      await tester.pumpWidget(wrap(
        Scaffold(body: ListView(children: [OnDutyRequestForm(onSubmitted: () {}, today: DateTime(2026, 10, 26))])),
        overridesFor(att: att, duty: duty),
      ),);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Submit on duty request'));
      await tester.pumpAndSettle();
      expect(find.text('Enter the purpose of the visit'), findsOneWidget);
      expect(duty.applied, isEmpty);
      // purpose given but no dates
      await tester.enterText(find.widgetWithText(TextFormField, 'Purpose'), 'Installation');
      await tester.tap(find.text('Submit on duty request'));
      await tester.pumpAndSettle();
      expect(find.text('Choose the from and to dates.'), findsOneWidget);
      expect(duty.applied, isEmpty);
    });

    testWidgets('form: submits dates, any location and purpose', (tester) async {
      tall(tester);
      final duty = FakeOnDutyRepo();
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      var done = 0;
      final today = DateTime.now();
      await tester.pumpWidget(wrap(
        Scaffold(body: ListView(children: [OnDutyRequestForm(onSubmitted: () => done++)])),
        overridesFor(att: att, duty: duty),
      ),);
      await tester.pumpAndSettle();
      await tester.tap(find.byType(PickerField).first);
      await tester.pumpAndSettle();
      await tester.tap(find.text('OK'));
      await tester.pumpAndSettle();
      await tester.enterText(find.widgetWithText(TextFormField, 'Purpose'), '  Installation at client  ');
      await tester.tap(find.text('Submit on duty request'));
      await tester.pumpAndSettle();
      expect(duty.applied.single.locationId, isNull);
      expect(duty.applied.single.purpose, '  Installation at client  ');
      expect(duty.applied.single.from.day, today.day);
      expect(done, 1);
    });

    testWidgets('form: server errors are shown in plain words', (tester) async {
      tall(tester);
      final duty = FakeOnDutyRepo()
        ..applyError = const ApiException(
          code: 'OVERLAPPING_REQUEST',
          message: 'x',
          status: 409,
          details: [FieldError('date', '2026-10-29')],
        );
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      await tester.pumpWidget(wrap(
        Scaffold(body: ListView(children: [OnDutyRequestForm(onSubmitted: () {})])),
        overridesFor(att: att, duty: duty),
      ),);
      await tester.pumpAndSettle();
      await tester.tap(find.byType(PickerField).first);
      await tester.pumpAndSettle();
      await tester.tap(find.text('OK'));
      await tester.pumpAndSettle();
      await tester.enterText(find.widgetWithText(TextFormField, 'Purpose'), 'Visit');
      await tester.tap(find.text('Submit on duty request'));
      await tester.pumpAndSettle();
      expect(find.textContaining('2026-10-29'), findsOneWidget);
    });

    testWidgets('My Requests lists an on-duty request and cancels it', (tester) async {
      tall(tester);
      final leave = _Leave()
        ..mine = [
          const RequestItem(
            kind: RequestKind.onDuty,
            id: 'od1',
            status: RequestStatus.pending,
            fromDate: '2026-11-03',
            toDate: '2026-11-05',
            days: 3,
            reason: 'Installation',
            locationName: 'Acme',
          ),
        ];
      final att = FakeAttendanceRepo()..cfg = configWith(onDutyOn);
      await tester.pumpWidget(wrap(const LeaveScreen(), leaveOverrides(att, FakeOnDutyRepo(), leave)));
      await tester.pumpAndSettle();
      await tester.tap(find.text('My Requests'));
      await tester.pumpAndSettle();
      expect(find.textContaining('On duty - 3 days'), findsOneWidget);
      await tester.tap(find.textContaining('On duty - 3 days'));
      await tester.pumpAndSettle();
      expect(find.text('Acme'), findsOneWidget);
      expect(find.text('Purpose'), findsOneWidget);
      expect(find.text('Installation'), findsOneWidget);
      await tester.tap(find.text('Cancel request'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Cancel request').last);
      await tester.pumpAndSettle();
      expect(leave.cancelled.single, (RequestKind.onDuty, 'od1'));
    });

    testWidgets('approvals: On duty segment loads that type and approves', (tester) async {
      tall(tester);
      final leave = _Leave()
        ..adminItems = [
          const RequestItem(
            kind: RequestKind.onDuty,
            id: 'od2',
            status: RequestStatus.pending,
            fromDate: '2026-11-03',
            toDate: '2026-11-03',
            days: 1,
            reason: 'Audit',
            user: PersonSummary(id: 'u9', employeeId: 'E9', fullName: 'Bala K'),
          ),
        ];
      await tester.pumpWidget(wrap(const ApprovalsScreen(), [
        leaveRepositoryProvider.overrideWithValue(leave),
        authControllerProvider.overrideWith(() => FakeAuth(userWith(['request.approve_all']))),
      ]),);
      await tester.pumpAndSettle();
      expect(find.text('On duty'), findsOneWidget);
      await tester.tap(find.text('On duty'));
      await tester.pumpAndSettle();
      expect(leave.queries.last, RequestKind.onDuty);
      expect(find.textContaining('Bala K'), findsWidgets);
      await tester.tap(find.textContaining('On duty - 1 day'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Approve'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Approve').last);
      await tester.pumpAndSettle();
      expect(leave.decisions.single, (RequestKind.onDuty, 'od2', true));
    });

    testWidgets('approvals: leave that overlaps an approved on-duty day warns the approver', (tester) async {
      tall(tester);
      final leave = _Leave()
        ..adminItems = [
          const RequestItem(
            kind: RequestKind.leave,
            id: 'l1',
            status: RequestStatus.pending,
            leaveTypeName: 'Casual Leave',
            fromDate: '2026-11-03',
            toDate: '2026-11-03',
            days: 1,
            reason: 'Family',
            onDutyConflict: true,
            user: PersonSummary(id: 'u9', employeeId: 'E9', fullName: 'Bala K'),
          ),
        ];
      await tester.pumpWidget(wrap(const ApprovalsScreen(), [
        leaveRepositoryProvider.overrideWithValue(leave),
        authControllerProvider.overrideWith(() => FakeAuth(userWith(['request.approve_all']))),
      ]),);
      await tester.pumpAndSettle();
      await tester.tap(find.textContaining('Casual Leave'));
      await tester.pumpAndSettle();
      expect(find.textContaining('approved On Duty request overlaps'), findsOneWidget);
      await tester.tap(find.text('Approve'));
      await tester.pumpAndSettle();
      expect(find.textContaining('approved On Duty request on these dates'), findsOneWidget);
    });

    testWidgets('RequestTile uses a distinct icon for on duty', (tester) async {
      await tester.pumpWidget(wrap(
        Scaffold(
          body: RequestTile(
            item: const RequestItem(
              kind: RequestKind.onDuty,
              id: 'x',
              status: RequestStatus.approved,
              fromDate: '2026-11-03',
              toDate: '2026-11-03',
              days: 1,
            ),
            onTap: () {},
          ),
        ),
        const [],
      ),);
      expect(find.byIcon(Icons.work_outline), findsOneWidget);
    });
  });
}

String onDutyDayLabelFor(String mode, List<String> names) => onDutyDayLabel(
      AttendanceRecord(
        id: 'x',
        workDate: '2026-10-26',
        status: DayStatus.present,
        workMode: mode,
        onDutyLocations: names,
      ),
    );

/// Office check-out refused because the server has an open visit.
class _VisitOpenAttendanceRepo extends FakeAttendanceRepo {
  _VisitOpenAttendanceRepo(FakeAttendanceRepo from) {
    cfg = from.cfg;
    todayRecord = from.todayRecord;
  }

  @override
  Future<AttendanceRecord> checkOut(LocationFix fix) async =>
      throw const ApiException(code: 'VISIT_OPEN', message: 'x', status: 409);
}

class _Leave extends LeaveRepository {
  _Leave() : super(Dio(), MemoryJsonCache());
  List<RequestItem> mine = [];
  List<RequestItem> adminItems = [];
  final List<(RequestKind, String)> cancelled = [];
  final List<(RequestKind, String, bool)> decisions = [];
  final List<RequestKind> queries = [];

  @override
  Future<Cached<List<LeaveType>>> types() async => const Cached([]);

  @override
  Future<Cached<BalanceData>> balance(int year) async => Cached(
        BalanceData(year: year, leave: const [], permission: const PermissionBalance()),
      );

  @override
  Future<Cached<List<RequestItem>>> myRequests() async => Cached(mine);

  @override
  Future<RequestItem> cancel(RequestKind kind, String id) async {
    cancelled.add((kind, id));
    mine = [];
    return RequestItem(kind: kind, id: id, status: RequestStatus.cancelled);
  }

  @override
  Future<Paged<RequestItem>> adminRequests({
    required RequestKind kind,
    RequestStatus? status,
    int page = 1,
  }) async {
    queries.add(kind);
    final items = [for (final i in adminItems) if (i.kind == kind) i];
    return Paged(items: items, page: 1, total: items.length, pageSize: 20);
  }

  @override
  Future<RequestItem> decide(RequestKind kind, String id, {required bool approve, String? comment}) async {
    decisions.add((kind, id, approve));
    return RequestItem(kind: kind, id: id, status: RequestStatus.approved);
  }
}
