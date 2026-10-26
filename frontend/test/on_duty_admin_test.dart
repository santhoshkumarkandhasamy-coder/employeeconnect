import 'package:dio/dio.dart';
import 'package:employee_connect/console/console_shell.dart';
import 'package:employee_connect/console/pages/attendance_day_page.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/admin/data/admin_repository.dart';
import 'package:employee_connect/features/admin/domain/admin_models.dart';
import 'package:employee_connect/features/admin/domain/ops_models.dart';
import 'package:employee_connect/features/admin/presentation/admin_home_screen.dart';
import 'package:employee_connect/features/admin/presentation/admin_widgets.dart' show adminPermissionsProvider;
import 'package:employee_connect/features/admin/presentation/employee_detail_screen.dart';
import 'package:employee_connect/features/admin/presentation/settings_screen.dart';
import 'package:employee_connect/features/attendance/data/location_service.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart' show Paged;
import 'package:employee_connect/features/auth/presentation/auth_controller.dart';
import 'package:employee_connect/features/on_duty/data/on_duty_repository.dart';
import 'package:employee_connect/features/on_duty/domain/duty_admin_models.dart';
import 'package:employee_connect/features/on_duty/presentation/admin/duty_admin_providers.dart';
import 'package:employee_connect/features/on_duty/presentation/admin/duty_assignments_screen.dart';
import 'package:employee_connect/features/on_duty/presentation/admin/duty_location_form_screen.dart';
import 'package:employee_connect/features/on_duty/presentation/admin/duty_locations_screen.dart';
import 'package:employee_connect/features/on_duty/presentation/admin/duty_visits_screen.dart';
import 'package:employee_connect/features/on_duty/presentation/device_position_reader.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/attendance_fakes.dart' show FakeLocation;
import 'helpers/module_fakes.dart' show FakeAuth, userWith;
import 'helpers/on_duty_fakes.dart';

class FakeAdminRepo2 extends AdminRepository {
  FakeAdminRepo2() : super(Dio());
  Employee? detail;
  Map<String, Object?> values = {'on_duty.enabled': true, 'on_duty.max_visits_per_day': 10};
  Map<String, Object?>? savedSettings;
  List<AttendanceDayRow> day = [];
  String? lastStatus;

  @override
  Future<Employee> employee(String id) async => detail!;

  @override
  Future<Map<String, Object?>> settings() async => values;

  @override
  Future<Map<String, Object?>> saveSettings(Map<String, Object?> changes) async {
    savedSettings = changes;
    return {...values, ...changes};
  }

  @override
  Future<List<NamedRef>> refs(String path) async => const [
        NamedRef(id: 'd1', name: 'IT'),
        NamedRef(id: 'd2', name: 'Finance'),
      ];

  @override
  Future<Paged<AttendanceDayRow>> attendanceDay({
    String? date,
    String status = 'all',
    String? q,
    int page = 1,
  }) async {
    lastStatus = status;
    return Paged(items: day, page: 1, total: day.length, pageSize: 30);
  }
}

List<Override> ov(FakeOnDutyRepo duty, List<String> perms, {FakeAdminRepo2? admin}) => [
      onDutyRepositoryProvider.overrideWithValue(duty),
      adminRepositoryProvider.overrideWithValue(admin ?? FakeAdminRepo2()),
      authControllerProvider.overrideWith(() => FakeAuth(userWith(perms))),
    ];

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

void tall(WidgetTester t, {double w = 800}) {
  t.view.physicalSize = Size(w, 2600);
  t.view.devicePixelRatio = 1;
  addTearDown(t.view.reset);
}

const manage = ['duty_location.manage'];

DutyLocationAdmin site({
  String id = 'a1',
  String name = 'Acme',
  String mode = 'selected',
  bool active = true,
  int count = 3,
  bool inUse = false,
}) =>
    DutyLocationAdmin(
      id: id,
      name: name,
      address: '12 Mount Rd',
      lat: 13.0067,
      lng: 80.2206,
      radiusM: 200,
      assignMode: mode,
      isActive: active,
      assignedCount: count,
      inUse: inUse,
    );

Finder field(String label) => find.widgetWithText(TextFormField, label);

void main() {
  group('locations list', () {
    testWidgets('needs duty_location.manage', (tester) async {
      final duty = FakeOnDutyRepo();
      await tester.pumpWidget(wrap(const DutyLocationsScreen(), ov(duty, ['attendance.view_all'])));
      await tester.pumpAndSettle();
      expect(find.text('You do not have permission to view this.'), findsOneWidget);
    });

    testWidgets('shows sites with radius, mode and count; empty and error states', (tester) async {
      tall(tester);
      final duty = FakeOnDutyRepo()..adminSites = [site(), site(id: 'a2', name: 'Branch', mode: 'all', active: false)];
      await tester.pumpWidget(wrap(const DutyLocationsScreen(), ov(duty, manage)));
      await tester.pumpAndSettle();
      expect(find.text('Acme'), findsOneWidget);
      expect(find.textContaining('Radius 200 m'), findsNWidgets(2));
      expect(find.text('Selected employees (3)'), findsOneWidget);
      expect(find.text('All employees (3)'), findsOneWidget);
      expect(find.textContaining('Inactive'), findsWidgets);

      duty.adminSites = [];
      await tester.pumpWidget(wrap(const DutyLocationsScreen(), ov(duty, manage)));
      await tester.pumpAndSettle();
      expect(find.textContaining('No on-duty locations yet'), findsOneWidget);

      duty.adminSitesError = const ApiException(code: ApiException.networkCode, message: 'No connection.');
      await tester.pumpWidget(wrap(const DutyLocationsScreen(), ov(duty, manage)));
      await tester.pumpAndSettle();
      expect(find.text('No connection.'), findsOneWidget);
      duty.adminSitesError = null;
      duty.adminSites = [site()];
      await tester.tap(find.text('Try again'));
      await tester.pumpAndSettle();
      expect(find.text('Acme'), findsOneWidget);
    });

    testWidgets('delete asks first; IN_USE says to deactivate', (tester) async {
      tall(tester);
      final duty = FakeOnDutyRepo()
        ..adminSites = [site(inUse: true)]
        ..deleteError = const ApiException(code: 'IN_USE', message: 'x', status: 409);
      await tester.pumpWidget(wrap(const DutyLocationsScreen(), ov(duty, manage)));
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('Actions'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Delete'));
      await tester.pumpAndSettle();
      expect(find.textContaining('cannot be deleted'), findsOneWidget);
      await tester.tap(find.text('Delete').last);
      await tester.pumpAndSettle();
      expect(find.textContaining('Deactivate it instead'), findsOneWidget);
      expect(duty.deleted, isEmpty);
    });

    testWidgets('delete removes an unused site', (tester) async {
      tall(tester);
      final duty = FakeOnDutyRepo()..adminSites = [site()];
      await tester.pumpWidget(wrap(const DutyLocationsScreen(), ov(duty, manage)));
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('Actions'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Delete'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Delete').last);
      await tester.pumpAndSettle();
      expect(duty.deleted, ['a1']);
    });
  });

  group('location form', () {
    Future<void> openForm(WidgetTester t, FakeOnDutyRepo duty, {CurrentPositionReader? reader, DutyLocationAdmin? existing}) async {
      tall(t);
      await t.pumpWidget(wrap(DutyLocationFormScreen(existing: existing, readPosition: reader), ov(duty, manage)));
      await t.pumpAndSettle();
    }

    testWidgets('pasting coordinates (or a maps link) fills latitude and longitude', (tester) async {
      await openForm(tester, FakeOnDutyRepo());
      await tester.enterText(find.widgetWithText(TextField, 'Paste coordinates or a maps link'), '12.9716, 77.5946');
      await tester.tap(find.byTooltip('Use pasted coordinates'));
      await tester.pumpAndSettle();
      expect(tester.widget<TextFormField>(field('Latitude')).controller!.text, '12.9716');
      expect(tester.widget<TextFormField>(field('Longitude')).controller!.text, '77.5946');

      await tester.enterText(
        find.widgetWithText(TextField, 'Paste coordinates or a maps link'),
        'https://www.google.com/maps/place/Acme/@13.0067,80.2206,17z',
      );
      await tester.tap(find.byTooltip('Use pasted coordinates'));
      await tester.pumpAndSettle();
      expect(tester.widget<TextFormField>(field('Latitude')).controller!.text, '13.0067');
      expect(tester.widget<TextFormField>(field('Longitude')).controller!.text, '80.2206');
    });

    testWidgets('unreadable paste shows a hint and leaves the fields alone', (tester) async {
      await openForm(tester, FakeOnDutyRepo());
      await tester.enterText(find.widgetWithText(TextField, 'Paste coordinates or a maps link'), 'https://maps.app.goo.gl/x');
      await tester.tap(find.byTooltip('Use pasted coordinates'));
      await tester.pumpAndSettle();
      expect(find.textContaining('Could not find coordinates'), findsOneWidget);
      expect(tester.widget<TextFormField>(field('Latitude')).controller!.text, isEmpty);
    });

    testWidgets('validation blocks the save: name, position, radius', (tester) async {
      final duty = FakeOnDutyRepo();
      await openForm(tester, duty);
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();
      expect(find.text('Enter at least 2 characters'), findsOneWidget);
      expect(find.text('Enter a number'), findsNWidgets(2));
      await tester.enterText(field('Name'), 'Acme');
      await tester.enterText(field('Latitude'), '95');
      await tester.enterText(field('Longitude'), '181');
      await tester.enterText(field('Radius (m)'), '5');
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();
      expect(find.text('Must be between -90 and 90'), findsOneWidget);
      expect(find.text('Must be between -180 and 180'), findsOneWidget);
      expect(find.text('Must be between 10 and 2000'), findsOneWidget);
      expect(duty.saved, isEmpty);
    });

    testWidgets('saves a valid location with the contract body', (tester) async {
      final duty = FakeOnDutyRepo();
      await openForm(tester, duty);
      await tester.enterText(field('Name'), '  Acme Client  ');
      await tester.enterText(field('Latitude'), '13.00671234');
      await tester.enterText(field('Longitude'), '80.2206');
      await tester.enterText(field('Radius (m)'), '250');
      await tester.tap(find.text('All employees'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();
      final (id, body) = duty.saved.single;
      expect(id, isNull);
      expect(body, {
        'name': 'Acme Client',
        'address': null,
        'lat': 13.006712,
        'lng': 80.2206,
        'radius_m': 250,
        'assign_mode': 'all',
        'is_active': true,
      });
    });

    testWidgets('editing replaces the same record; duplicate name shows under the field', (tester) async {
      final duty = FakeOnDutyRepo()
        ..saveError = const ApiException(
          code: 'CONFLICT',
          message: 'Name exists',
          status: 409,
          details: [FieldError('name', 'A location with this name already exists')],
        );
      await openForm(tester, duty, existing: site());
      await tester.enterText(field('Name'), 'Branch');
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();
      expect(duty.saved.single.$1, 'a1');
      expect(find.text('A location with this name already exists'), findsWidgets);
    });

    testWidgets('"Use my current location" exists only when a reader is supplied (phone app)', (tester) async {
      await openForm(tester, FakeOnDutyRepo());
      expect(find.text('Use my current location'), findsNothing);

      await openForm(tester, FakeOnDutyRepo(), reader: () async => (lat: 12.123456789, lng: 77.5));
      expect(find.text('Use my current location'), findsOneWidget);
      await tester.tap(find.text('Use my current location'));
      await tester.pumpAndSettle();
      expect(tester.widget<TextFormField>(field('Latitude')).controller!.text, '12.123457');
      expect(tester.widget<TextFormField>(field('Longitude')).controller!.text, '77.5');
    });

    testWidgets('a failed position read is explained and typing still works', (tester) async {
      await openForm(
        tester,
        FakeOnDutyRepo(),
        reader: () async => throw const PositionReadException('Turn on location services to use your position.'),
      );
      await tester.tap(find.text('Use my current location'));
      await tester.pumpAndSettle();
      expect(find.text('Turn on location services to use your position.'), findsOneWidget);
    });

    testWidgets('the phone reader asks once, reads once and rejects imprecise fixes', (tester) async {
      final loc = FakeLocation(state: LocationAccess.denied, afterRequest: LocationAccess.granted);
      final p = await devicePositionReader(loc)();
      expect(p.lat, 12.97);
      expect(loc.requests, 1);

      final blocked = FakeLocation(state: LocationAccess.deniedForever);
      await expectLater(devicePositionReader(blocked)(), throwsA(isA<PositionReadException>()));
      expect(blocked.requests, 0);

      final coarse = FakeLocation(fix: LocationFix(lat: 1, lng: 1, accuracyM: 900, timestamp: DateTime.now()));
      await expectLater(devicePositionReader(coarse)(), throwsA(isA<PositionReadException>()));
    });
  });

  group('assignments', () {
    testWidgets('search, tick, add a department, switch to all, save', (tester) async {
      tall(tester);
      final duty = FakeOnDutyRepo()
        ..assignmentData = const DutyAssignments(
          locationId: 'a1',
          assignMode: 'selected',
          users: [EmployeeRef(id: '1', employeeId: 'E1', fullName: 'Asha Rao')],
        )
        ..employeesFound = const [
          EmployeeRef(id: '2', employeeId: 'E2', fullName: 'Bala K'),
          EmployeeRef(id: '1', employeeId: 'E1', fullName: 'Asha Rao'),
        ];
      await tester.pumpWidget(wrap(DutyAssignmentsScreen(location: site()), ov(duty, manage)));
      await tester.pumpAndSettle();
      expect(find.text('Assigned (1)'), findsOneWidget);

      await tester.enterText(find.widgetWithText(TextField, 'Search by name or ID'), 'bal');
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(CheckboxListTile, 'Bala K (E2)'));
      await tester.pumpAndSettle();
      expect(find.text('Assigned (2)'), findsOneWidget);

      await tester.tap(find.text('Add department'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(CheckboxListTile, 'Finance'));
      await tester.tap(find.text('Done'));
      await tester.pumpAndSettle();
      expect(find.widgetWithText(InputChip, 'Finance'), findsOneWidget);

      await tester.tap(find.text('All employees'));
      await tester.pumpAndSettle();
      expect(find.textContaining('Every active employee'), findsOneWidget);

      await tester.tap(find.text('Save assignments'));
      await tester.pumpAndSettle();
      expect(duty.savedAssignments.single.toBody(), {
        'assign_mode': 'all',
        'user_ids': ['1', '2'],
        'add_department_ids': ['d2'],
      });
    });

    testWidgets('removing a department chip and an employee, and a server error', (tester) async {
      tall(tester);
      final duty = FakeOnDutyRepo()
        ..assignmentData = const DutyAssignments(
          locationId: 'a1',
          assignMode: 'selected',
          users: [
            EmployeeRef(id: '1', employeeId: 'E1', fullName: 'Asha Rao'),
            EmployeeRef(id: '2', employeeId: 'E2', fullName: 'Bala K'),
          ],
        )
        ..assignError = const ApiException(
          code: 'VALIDATION_ERROR',
          message: 'bad',
          status: 422,
          details: [FieldError('user_ids', 'not-a-uuid')],
        );
      await tester.pumpWidget(wrap(DutyAssignmentsScreen(location: site()), ov(duty, manage)));
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('Remove Asha Rao'));
      await tester.pumpAndSettle();
      expect(find.text('Assigned (1)'), findsOneWidget);
      await tester.tap(find.text('Save assignments'));
      await tester.pumpAndSettle();
      expect(find.textContaining('not-a-uuid'), findsOneWidget);
      expect(find.text('Save assignments'), findsOneWidget, reason: 'stays on the screen to retry');
    });

    testWidgets('empty selection says so; search below 2 characters does not call the server', (tester) async {
      tall(tester);
      final duty = FakeOnDutyRepo();
      await tester.pumpWidget(wrap(DutyAssignmentsScreen(location: site()), ov(duty, manage)));
      await tester.pumpAndSettle();
      expect(find.text('No employees chosen yet.'), findsOneWidget);
      await tester.enterText(find.widgetWithText(TextField, 'Search by name or ID'), 'a');
      await tester.pumpAndSettle();
      expect(find.text('No employees match.'), findsNothing);
    });
  });

  group('visits', () {
    AdminVisit visit({bool detail = true}) => AdminVisit.fromJson({
          'id': 'v1',
          'work_date': '2026-10-26',
          'location': {'id': 'l1', 'name': 'Acme'},
          'check_in_at': '2026-10-26T05:12:00Z',
          'check_out_at': '2026-10-26T06:47:00Z',
          'visit_minutes': 95,
          'open': false,
          'auto_closed': false,
          'note': 'Quarterly review',
          'user': {'id': 'u1', 'employee_id': 'E1', 'full_name': 'Asha Rao'},
          'check_in_distance_m': detail ? 12 : null,
          'check_out_distance_m': detail ? 340 : null,
          'check_out_within_fence': detail ? false : null,
          'suspicious': detail,
        });

    testWidgets('view_all sees distances, the away-from-site flag and suspicious', (tester) async {
      tall(tester);
      final duty = FakeOnDutyRepo()..visitData = [visit()];
      await tester.pumpWidget(wrap(const DutyVisitsScreen(), ov(duty, ['attendance.view_all'])));
      await tester.pumpAndSettle();
      expect(find.textContaining('Asha Rao'), findsOneWidget);
      expect(find.textContaining('In 12 m from site'), findsOneWidget);
      expect(find.textContaining('Out 340 m from site'), findsOneWidget);
      expect(find.text('Checked out away from site'), findsOneWidget);
      expect(find.textContaining('Suspicious'), findsOneWidget);
    });

    testWidgets('a manager (view_team) never sees distances or flags, even if sent', (tester) async {
      tall(tester);
      final duty = FakeOnDutyRepo()..visitData = [visit()];
      await tester.pumpWidget(wrap(const DutyVisitsScreen(), ov(duty, ['attendance.view_team'])));
      await tester.pumpAndSettle();
      expect(find.textContaining('Asha Rao'), findsOneWidget);
      expect(find.textContaining('from site'), findsNothing);
      expect(find.textContaining('Suspicious'), findsNothing);
      expect(find.text('Checked out away from site'), findsNothing);
      expect(find.textContaining('Quarterly review'), findsOneWidget);
    });

    testWidgets('null detail fields (manager response) render cleanly', (tester) async {
      tall(tester);
      final duty = FakeOnDutyRepo()..visitData = [visit(detail: false)];
      await tester.pumpWidget(wrap(const DutyVisitsScreen(), ov(duty, ['attendance.view_all'])));
      await tester.pumpAndSettle();
      expect(find.textContaining('from site'), findsNothing);
      expect(tester.takeException(), isNull);
    });

    testWidgets('no permission, empty day and error with retry', (tester) async {
      tall(tester);
      final duty = FakeOnDutyRepo();
      await tester.pumpWidget(wrap(const DutyVisitsScreen(), ov(duty, ['employee.view'])));
      await tester.pumpAndSettle();
      expect(find.text('You do not have permission to view this.'), findsOneWidget);
      expect(duty.visitQueries, isEmpty, reason: 'no request without the permission');

      await tester.pumpWidget(wrap(const DutyVisitsScreen(), ov(duty, ['attendance.view_all'])));
      await tester.pumpAndSettle();
      expect(find.textContaining('No visits on'), findsOneWidget);

      duty.visitsError = const ApiException(code: ApiException.networkCode, message: 'No connection.');
      await tester.pumpWidget(wrap(const DutyVisitsScreen(), ov(duty, ['attendance.view_all'])));
      await tester.pumpAndSettle();
      expect(find.text('No connection.'), findsOneWidget);
      duty.visitsError = null;
      duty.visitData = [visit()];
      await tester.tap(find.text('Try again'));
      await tester.pumpAndSettle();
      expect(find.textContaining('Asha Rao'), findsOneWidget);
    });

    testWidgets('filters go to the server: open only and employee id', (tester) async {
      tall(tester);
      final duty = FakeOnDutyRepo();
      await tester.pumpWidget(wrap(const DutyVisitsScreen(), ov(duty, ['attendance.view_all'])));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Open now'));
      await tester.pumpAndSettle();
      expect(duty.visitQueries.last.openOnly, isTrue);
      await tester.enterText(find.widgetWithText(TextField, 'Employee ID'), 'e1');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();
      expect(duty.visitQueries.last.employeeId, 'e1');
    });
  });

  group('admin home, settings and employee detail', () {
    testWidgets('admin home lists the On Duty sections by permission', (tester) async {
      tall(tester);
      await tester.pumpWidget(wrap(const AdminHomeScreen(), ov(FakeOnDutyRepo(), [...manage, 'attendance.view_team'])));
      await tester.pumpAndSettle();
      expect(find.text('On-duty locations'), findsOneWidget);
      expect(find.text('On-duty visits'), findsOneWidget);

      await tester.pumpWidget(wrap(const AdminHomeScreen(), ov(FakeOnDutyRepo(), ['employee.view'])));
      await tester.pumpAndSettle();
      expect(find.text('On-duty locations'), findsNothing);
      expect(find.text('On-duty visits'), findsNothing);
    });

    testWidgets('settings: the On Duty group saves only the changed key', (tester) async {
      tall(tester);
      final admin = FakeAdminRepo2();
      await tester.pumpWidget(wrap(const SettingsScreen(), ov(FakeOnDutyRepo(), ['settings.manage'], admin: admin)));
      await tester.pumpAndSettle();
      expect(find.text('On Duty'), findsOneWidget);
      expect(find.text('On Duty enabled'), findsOneWidget);
      await tester.enterText(field('Max visits per day'), '60');
      await tester.tap(find.text('Save settings'));
      await tester.pumpAndSettle();
      expect(find.text('Must be between 1 and 50'), findsOneWidget);
      expect(admin.savedSettings, isNull);
      await tester.enterText(field('Max visits per day'), '12');
      await tester.tap(find.text('Save settings'));
      await tester.pumpAndSettle();
      expect(admin.savedSettings, {'on_duty.max_visits_per_day': 12});
    });

    testWidgets('employee detail lists the sites that apply (read-only), only with the permission', (tester) async {
      tall(tester);
      const emp = Employee(
        id: 'e1',
        employeeId: 'E1',
        fullName: 'Asha Rao',
        officialEmail: 'a@corp.com',
        department: NamedRef(id: 'd1', name: 'IT'),
        role: NamedRef(id: 'r1', name: 'employee'),
      );
      final admin = FakeAdminRepo2()..detail = emp;
      final duty = FakeOnDutyRepo()
        ..employeeSites = const [
          EmployeeDutyLocation(id: 'l1', name: 'Acme', assignMode: 'all'),
          EmployeeDutyLocation(id: 'l2', name: 'Old Site', isActive: false),
        ];
      await tester.pumpWidget(
        wrap(const EmployeeDetailScreen(employeeId: 'e1'), ov(duty, ['employee.view', ...manage], admin: admin)),
      );
      await tester.pumpAndSettle();
      expect(find.text('On-duty locations'), findsOneWidget);
      expect(find.textContaining('Acme - all employees'), findsOneWidget);
      expect(find.textContaining('Old Site (inactive)'), findsOneWidget);

      await tester.pumpWidget(
        wrap(const EmployeeDetailScreen(employeeId: 'e1'), ov(duty, ['employee.view'], admin: admin)),
      );
      await tester.pumpAndSettle();
      expect(find.text('On-duty locations'), findsNothing);
    });
  });

  group('web console', () {
    testWidgets('destinations follow permissions (server still enforces)', (tester) async {
      List<String> labels(List<String> p) => [for (final d in visibleDestinations(p)) d.label];
      expect(labels(const []), ['Dashboard']);
      expect(labels(const ['attendance.view_all']), containsAll(['On-duty visits', 'Attendance today']));
      expect(labels(const ['attendance.view_team']), contains('On-duty visits'));
      expect(labels(const ['attendance.view_team']), isNot(contains('Attendance today')));
      expect(labels(const ['duty_location.manage']), contains('On-duty locations'));
      expect(labels(const ['employee.view']), isNot(contains('On-duty locations')));
      expect(labels(const ['request.approve_team']), contains('Requests'));
      expect(labels(const ['request.view_all']), contains('Requests'));
      expect(labels(const ['attendance.view_own']), isNot(contains('Requests')));
      expect(labels(const ['settings.manage']), contains('Settings'));
    });

    testWidgets('attendance day page shows Mode and an On Duty filter', (tester) async {
      tall(tester, w: 1200);
      final admin = FakeAdminRepo2()
        ..day = [
          const AttendanceDayRow(
            user: EmployeeRef(id: 'u1', employeeId: 'E1', fullName: 'Asha Rao'),
            status: 'present',
            workMode: 'mixed',
            onDutyLocations: ['Acme', 'Branch'],
          ),
          const AttendanceDayRow(
            user: EmployeeRef(id: 'u2', employeeId: 'E2', fullName: 'Bala K'),
            status: 'not_checked_in',
          ),
        ];
      await tester.pumpWidget(wrap(
        const Scaffold(body: AttendanceDayPage()),
        [adminRepositoryProvider.overrideWithValue(admin)],
      ),);
      await tester.pumpAndSettle();
      expect(find.text('Mode: Office + On Duty'), findsOneWidget);
      expect(find.text('Acme, Branch'), findsOneWidget);
      expect(find.textContaining('Mode:'), findsOneWidget, reason: 'no mode for an employee without a record');
      await tester.tap(find.widgetWithText(ChoiceChip, 'On Duty'));
      await tester.pumpAndSettle();
      expect(admin.lastStatus, 'on_duty');
    });

    testWidgets('locations page works without any location plugin (no "use my location")', (tester) async {
      tall(tester, w: 1200);
      final duty = FakeOnDutyRepo()..adminSites = [site()];
      await tester.pumpWidget(wrap(
        const Scaffold(body: DutyLocationsScreen()),
        [
          onDutyRepositoryProvider.overrideWithValue(duty),
          adminPermissionsProvider.overrideWithValue(manage),
        ],
      ),);
      await tester.pumpAndSettle();
      expect(find.text('Acme'), findsOneWidget);
      await tester.tap(find.text('Add location'));
      await tester.pumpAndSettle();
      expect(find.text('Add location'), findsWidgets);
      expect(find.text('Use my current location'), findsNothing);
      expect(find.widgetWithText(TextField, 'Paste coordinates or a maps link'), findsOneWidget);
    });

    testWidgets('visits page embeds in the shell body', (tester) async {
      tall(tester, w: 1200);
      final duty = FakeOnDutyRepo();
      await tester.pumpWidget(wrap(
        const Scaffold(body: DutyVisitsScreen(embedded: true)),
        [
          onDutyRepositoryProvider.overrideWithValue(duty),
          adminPermissionsProvider.overrideWithValue(const ['attendance.view_all']),
        ],
      ),);
      await tester.pumpAndSettle();
      expect(find.textContaining('No visits on'), findsOneWidget);
    });
  });
}
