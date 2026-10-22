import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/admin/data/admin_repository.dart';
import 'package:employee_connect/features/admin/domain/admin_models.dart';
import 'package:employee_connect/features/admin/domain/ops_models.dart';
import 'package:employee_connect/features/admin/presentation/admin_home_screen.dart';
import 'package:employee_connect/features/admin/presentation/admin_widgets.dart' show showTempPasswordDialog;
import 'package:employee_connect/features/admin/presentation/employee_detail_screen.dart';
import 'package:employee_connect/features/admin/presentation/employees_screen.dart';
import 'package:employee_connect/features/admin/presentation/master_screen.dart';
import 'package:employee_connect/features/admin/presentation/settings_screen.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart' show Paged;
import 'package:employee_connect/features/auth/presentation/auth_controller.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/module_fakes.dart';

class FakeAdminRepo extends AdminRepository {
  FakeAdminRepo() : super(Dio());

  List<Employee> employeeList = [];
  ApiException? listError;
  Employee? detail;
  final List<String> resetIds = [];
  final List<String?> resetPasswords = [];
  /// When true, a reset without current_password answers 422 like the server
  /// does for a target that holds permissions.
  bool requirePassword = false;
  final List<String> activations = [];
  List<RoleItem> roleList = [];
  int wrongPasswordsLeft = 0;
  final List<String> deletedIds = [];
  int listCalls = 0;

  @override
  Future<Paged<Employee>> employees(EmployeeQuery q, {int page = 1}) async {
    listCalls++;
    final e = listError;
    if (e != null) throw e;
    return Paged(items: employeeList, page: 1, total: employeeList.length, pageSize: 20);
  }

  @override
  Future<List<NamedRef>> refs(String path) async => const [NamedRef(id: 'd1', name: 'IT')];

  @override
  Future<Employee> employee(String id) async => detail!;

  @override
  Future<String> resetPassword(String id, {String? currentPassword}) async {
    resetPasswords.add(currentPassword);
    if (requirePassword && currentPassword == null) {
      throw const ApiException(
        code: 'VALIDATION_ERROR',
        message: 'Invalid',
        status: 422,
        details: [FieldError('current_password', 'Required')],
      );
    }
    if (wrongPasswordsLeft > 0) {
      wrongPasswordsLeft--;
      throw const ApiException(code: 'REAUTH_FAILED', message: 'Wrong password', status: 403);
    }
    resetIds.add(id);
    return 'Xk9#TempPw';
  }

  @override
  Future<List<RoleItem>> roles() async => roleList;

  @override
  Future<void> setEmployeeActive(
    String id, {
    required bool active,
    DeactivationReason? reason,
  }) async {
    activations.add('$id:$active');
  }

  @override
  Future<void> deleteEmployee(String id) async => deletedIds.add(id);
}

Employee emp(String id, String name) => Employee(
      id: id,
      employeeId: 'E-$id',
      fullName: name,
      officialEmail: '$id@corp.com',
      department: const NamedRef(id: 'd1', name: 'IT'),
      role: const NamedRef(id: 'r1', name: 'employee'),
    );

/// Target that holds permissions (role name other than `employee`), so the
/// password prompt is shown up front.
Employee hrEmp(String id, String name) => Employee(
      id: id,
      employeeId: 'E-$id',
      fullName: name,
      officialEmail: '$id@corp.com',
      department: const NamedRef(id: 'd1', name: 'IT'),
      role: const NamedRef(id: 'r2', name: 'hr'),
    );

List<Override> _o(FakeAdminRepo repo, List<String> perms) => [
      adminRepositoryProvider.overrideWithValue(repo),
      authControllerProvider.overrideWith(() => FakeAuth(userWith(perms))),
    ];

void _tall(WidgetTester t) {
  t.view.physicalSize = const Size(800, 2400);
  t.view.devicePixelRatio = 1;
  addTearDown(t.view.reset);
}

void main() {
  group('admin home', () {
    testWidgets('lists only permitted sections', (tester) async {
      await tester.pumpWidget(app(
        const AdminHomeScreen(),
        _o(FakeAdminRepo(), ['employee.view', 'audit.view']),
      ),);
      await tester.pumpAndSettle();
      expect(find.text('Employees'), findsOneWidget);
      expect(find.text('Audit log'), findsOneWidget);
      expect(find.text('Settings'), findsNothing);
      expect(find.text('Roles and permissions'), findsNothing);
    });

    testWidgets('no permissions shows an explanation', (tester) async {
      await tester.pumpWidget(app(const AdminHomeScreen(), _o(FakeAdminRepo(), const [])));
      await tester.pumpAndSettle();
      expect(find.textContaining('do not have access'), findsOneWidget);
    });
  });

  group('employees list', () {
    testWidgets('without employee.view no request is made', (tester) async {
      final repo = FakeAdminRepo();
      await tester.pumpWidget(app(const EmployeesScreen(), _o(repo, ['settings.manage'])));
      await tester.pumpAndSettle();
      expect(find.textContaining('do not have permission'), findsOneWidget);
      expect(repo.listCalls, 0);
    });

    testWidgets('shows rows; Add button needs employee.create', (tester) async {
      final repo = FakeAdminRepo()..employeeList = [emp('1', 'Asha Rao'), emp('2', 'Ravi K')];
      await tester.pumpWidget(app(const EmployeesScreen(), _o(repo, ['employee.view'])));
      await tester.pumpAndSettle();
      expect(find.text('Asha Rao'), findsOneWidget);
      expect(find.text('Ravi K'), findsOneWidget);
      expect(find.text('Add'), findsNothing);

      await tester.pumpWidget(app(
        const EmployeesScreen(),
        [..._o(repo, ['employee.view', 'employee.create'])],
      ),);
      await tester.pumpAndSettle();
      expect(find.text('Add'), findsOneWidget);
    });

    testWidgets('empty and error states', (tester) async {
      final repo = FakeAdminRepo();
      await tester.pumpWidget(app(const EmployeesScreen(), _o(repo, ['employee.view'])));
      await tester.pumpAndSettle();
      expect(find.text('No employees yet.'), findsOneWidget);

      final failing = FakeAdminRepo()
        ..listError = const ApiException(code: ApiException.networkCode, message: 'No connection.');
      await tester.pumpWidget(app(
        const EmployeesScreen(),
        _o(failing, ['employee.view']),
        mode: ThemeMode.dark,
      ),);
      await tester.pumpAndSettle();
      expect(find.text('No connection.'), findsOneWidget);
      failing.listError = null;
      failing.employeeList = [emp('3', 'Back Again')];
      await tester.tap(find.text('Try again'));
      await tester.pumpAndSettle();
      expect(find.text('Back Again'), findsOneWidget);
    });
  });

  group('employee detail', () {
    const perms = [
      'employee.view',
      'employee.update',
      'employee.deactivate',
      'employee.delete',
      'employee.reset_password',
    ];

    testWidgets('own record cannot be deleted or deactivated', (tester) async {
      _tall(tester);
      final repo = FakeAdminRepo()..detail = emp('u1', 'Asha Rao'); // userWith() has id u1
      await tester.pumpWidget(app(const EmployeeDetailScreen(employeeId: 'u1'), _o(repo, perms)));
      await tester.pumpAndSettle();
      expect(find.text('Edit details'), findsOneWidget);
      expect(find.text('Delete employee'), findsNothing);
      expect(find.text('Deactivate'), findsNothing);
    });

    testWidgets('plain employee target: reset sends no password and shows no prompt',
        (tester) async {
      _tall(tester);
      final repo = FakeAdminRepo()..detail = emp('9', 'Ravi K');
      await tester.pumpWidget(app(const EmployeeDetailScreen(employeeId: '9'), _o(repo, perms)));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Reset password'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(FilledButton, 'Reset'));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('reauth-password')), findsNothing);
      expect(repo.resetPasswords, [null]);
      expect(find.text('Xk9#TempPw'), findsOneWidget);
    });

    testWidgets('a 422 for current_password falls back to the prompt', (tester) async {
      _tall(tester);
      final repo = FakeAdminRepo()
        ..detail = emp('9', 'Ravi K')
        ..requirePassword = true;
      await tester.pumpWidget(app(const EmployeeDetailScreen(employeeId: '9'), _o(repo, perms)));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Reset password'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(FilledButton, 'Reset'));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('reauth-password')), findsOneWidget);
      await tester.enterText(find.byKey(const ValueKey('reauth-password')), 'My0wn!Passw0rd');
      await tester.tap(find.widgetWithText(FilledButton, 'Confirm'));
      await tester.pumpAndSettle();
      expect(repo.resetPasswords, [null, 'My0wn!Passw0rd']);
      expect(find.text('Xk9#TempPw'), findsOneWidget);
    });

    testWidgets('reset password confirms, then shows the temporary password once', (tester) async {
      _tall(tester);
      final repo = FakeAdminRepo()..detail = hrEmp('9', 'Ravi K');
      await tester.pumpWidget(app(const EmployeeDetailScreen(employeeId: '9'), _o(repo, perms)));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Reset password'));
      await tester.pumpAndSettle();
      expect(repo.resetIds, isEmpty);
      await tester.tap(find.widgetWithText(FilledButton, 'Reset'));
      await tester.pumpAndSettle();
      // Step-up re-auth: nothing is sent until the user's own password is given.
      expect(repo.resetIds, isEmpty);
      await tester.tap(find.widgetWithText(FilledButton, 'Confirm'));
      await tester.pumpAndSettle();
      expect(find.text('Enter your password'), findsOneWidget);
      await tester.enterText(find.byKey(const ValueKey('reauth-password')), 'My0wn!Passw0rd');
      await tester.tap(find.widgetWithText(FilledButton, 'Confirm'));
      await tester.pumpAndSettle();
      expect(repo.resetPasswords, ['My0wn!Passw0rd']);
      expect(repo.resetIds, ['9']);
      expect(find.byKey(const ValueKey('temp-password')), findsOneWidget);
      expect(find.text('Xk9#TempPw'), findsOneWidget);
      await tester.tap(find.text('Done'));
      await tester.pumpAndSettle();
      expect(find.text('Xk9#TempPw'), findsNothing);
    });

    testWidgets('a wrong password re-prompts; cancelling sends nothing more', (tester) async {
      _tall(tester);
      final repo = FakeAdminRepo()
        ..detail = hrEmp('9', 'Ravi K')
        ..wrongPasswordsLeft = 1;
      await tester.pumpWidget(app(const EmployeeDetailScreen(employeeId: '9'), _o(repo, perms)));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Reset password'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(FilledButton, 'Reset'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byKey(const ValueKey('reauth-password')), 'wrong');
      await tester.tap(find.widgetWithText(FilledButton, 'Confirm'));
      await tester.pumpAndSettle();
      expect(find.text('That password is not correct.'), findsOneWidget);
      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();
      expect(repo.resetPasswords, ['wrong']);
      expect(repo.resetIds, isEmpty);
      expect(find.byKey(const ValueKey('temp-password')), findsNothing);
    });

    testWidgets('Reset password is hidden on the own record', (tester) async {
      _tall(tester);
      final repo = FakeAdminRepo()..detail = emp('u1', 'Asha Rao');
      await tester.pumpWidget(app(const EmployeeDetailScreen(employeeId: 'u1'), _o(repo, perms)));
      await tester.pumpAndSettle();
      expect(find.text('Reset password'), findsNothing);
    });

    testWidgets('Activate asks for confirmation', (tester) async {
      _tall(tester);
      final repo = FakeAdminRepo()
        ..detail = const Employee(
          id: '9',
          employeeId: 'E-9',
          fullName: 'Ravi K',
          status: 'inactive',
          role: NamedRef(id: 'r1', name: 'employee'),
        );
      await tester.pumpWidget(app(const EmployeeDetailScreen(employeeId: '9'), _o(repo, perms)));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Activate'));
      await tester.pumpAndSettle();
      expect(repo.activations, isEmpty);
      await tester.tap(find.widgetWithText(FilledButton, 'Activate'));
      await tester.pumpAndSettle();
      expect(repo.activations, ['9:true']);
    });

    testWidgets('status actions are hidden for permission-holding targets without role.assign',
        (tester) async {
      _tall(tester);
      final repo = FakeAdminRepo()
        ..detail = const Employee(
          id: '9',
          employeeId: 'E-9',
          fullName: 'Hana HR',
          isLocked: true,
          role: NamedRef(id: 'hr', name: 'hr'),
        )
        ..roleList = const [
          RoleItem(id: 'hr', name: 'hr', permissionCodes: ['employee.view']),
        ];
      const withUnlock = [...perms, 'employee.unlock', 'role.view'];
      await tester.pumpWidget(app(const EmployeeDetailScreen(employeeId: '9'), _o(repo, withUnlock)));
      await tester.pumpAndSettle();
      expect(find.text('Deactivate'), findsNothing);
      expect(find.text('Unlock account'), findsNothing);
      expect(find.text('Edit details'), findsOneWidget);

      await tester.pumpWidget(app(
        const EmployeeDetailScreen(employeeId: '9'),
        _o(repo, [...withUnlock, 'role.assign']),
      ),);
      await tester.pumpAndSettle();
      expect(find.text('Deactivate'), findsOneWidget);
      expect(find.text('Unlock account'), findsOneWidget);
    });

    testWidgets('reduced employee shape hides the sign-in rows', (tester) async {
      _tall(tester);
      final repo = FakeAdminRepo()
        ..detail = Employee.fromJson(const {'id': '9', 'employee_id': 'E-9', 'full_name': 'Report'});
      await tester.pumpWidget(app(const EmployeeDetailScreen(employeeId: '9'), _o(repo, perms)));
      await tester.pumpAndSettle();
      expect(find.text('Last sign-in'), findsNothing);
      expect(find.text('Report'), findsOneWidget);
    });

    testWidgets('temporary password clipboard is cleared after the timeout and on close',
        (tester) async {
      _tall(tester);
      String? clip;
      tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(SystemChannels.platform,
          (call) async {
        if (call.method == 'Clipboard.setData') {
          clip = (call.arguments as Map)['text'] as String?;
        } else if (call.method == 'Clipboard.getData') {
          return <String, Object?>{'text': clip};
        }
        return null;
      });
      addTearDown(() => tester.binding.defaultBinaryMessenger
          .setMockMethodCallHandler(SystemChannels.platform, null),);
      await tester.pumpWidget(app(
        Builder(
          builder: (context) => Scaffold(
            body: TextButton(
              onPressed: () => showTempPasswordDialog(
                context,
                employeeLabel: 'Ravi',
                password: 'Xk9#TempPw',
              ),
              child: const Text('open'),
            ),
          ),
        ),
        const [],
      ),);
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Copy'));
      await tester.pump();
      expect(clip, 'Xk9#TempPw');
      await tester.pump(const Duration(seconds: 61));
      await tester.pump();
      expect(clip, '');

      // Closing the dialog also clears a copied password that is still there.
      await tester.tap(find.text('Copy'));
      await tester.pump();
      expect(clip, 'Xk9#TempPw');
      await tester.tap(find.text('Done'));
      await tester.pumpAndSettle();
      expect(clip, '');
    });

    testWidgets('delete needs confirmation', (tester) async {
      _tall(tester);
      final repo = FakeAdminRepo()..detail = emp('9', 'Ravi K');
      await tester.pumpWidget(app(
        Builder(
          builder: (context) => Scaffold(
            body: TextButton(
              onPressed: () => Navigator.of(context).push(MaterialPageRoute<void>(
                builder: (_) => const EmployeeDetailScreen(employeeId: '9'),
              ),),
              child: const Text('open'),
            ),
          ),
        ),
        _o(repo, perms),
      ),);
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Delete employee'));
      await tester.pumpAndSettle();
      expect(repo.deletedIds, isEmpty);
      await tester.tap(find.widgetWithText(FilledButton, 'Delete'));
      await tester.pumpAndSettle();
      expect(repo.deletedIds, ['9']);
      expect(find.text('open'), findsOneWidget); // popped back
    });
  });

  group('master form', () {
    Widget host(
      Future<String?> Function(Map<String, Object?>) onSave,
      MasterSpec spec, {
      Map<String, dynamic>? initial,
    }) =>
        app(
          Builder(
            builder: (context) => Scaffold(
              body: TextButton(
                onPressed: () => showDialog<bool>(
                  context: context,
                  builder: (_) => MasterFormDialog(spec: spec, onSave: onSave, initial: initial),
                ),
                child: const Text('open'),
              ),
            ),
          ),
          const [],
        );

    testWidgets('validates before saving, then sends the body', (tester) async {
      _tall(tester);
      final saved = <Map<String, Object?>>[];
      await tester.pumpWidget(host((b) async {
        saved.add(b);
        return null;
      }, departmentSpec,),);
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();
      expect(find.text('Enter name'), findsOneWidget);
      expect(find.text('Enter code'), findsOneWidget);
      expect(saved, isEmpty);

      await tester.enterText(find.widgetWithText(TextFormField, 'Name'), 'Finance');
      await tester.enterText(find.widgetWithText(TextFormField, 'Code'), 'FIN');
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();
      expect(saved.single, {'name': 'Finance', 'code': 'FIN', 'is_active': true});
      expect(find.text('Add department'), findsNothing); // dialog closed
    });

    testWidgets('server error (IN_USE style) is shown and the dialog stays open', (tester) async {
      _tall(tester);
      await tester.pumpWidget(host((_) async => 'It is still in use.', departmentSpec));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();
      await tester.enterText(find.widgetWithText(TextFormField, 'Name'), 'Finance');
      await tester.enterText(find.widgetWithText(TextFormField, 'Code'), 'FIN');
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();
      expect(find.text('It is still in use.'), findsOneWidget);
    });

    testWidgets('shift needs at least one working day', (tester) async {
      _tall(tester);
      await tester.pumpWidget(host(
        (_) async => null,
        shiftSpec,
        initial: const {
          'name': 'Day',
          'start_time': '09:00:00',
          'end_time': '18:00',
          'grace_minutes': 0,
          'working_days': <int>[],
          'is_active': true,
        },
      ),);
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();
      expect(find.text('Choose at least one working day'), findsOneWidget);
    });
  });

  group('settings form', () {
    testWidgets('range errors block saving; only changed keys are sent', (tester) async {
      _tall(tester);
      final sent = <Map<String, Object?>>[];
      await tester.pumpWidget(app(
        Scaffold(
          body: SettingsForm(
            initial: const {'office.radius_m': 200, 'food.cutoff_time': '18:00'},
            onSave: (c) async {
              sent.add(c);
              return null;
            },
          ),
        ),
        const [],
      ),);
      final radius = find.widgetWithText(TextFormField, 'Office radius (m)');
      await tester.enterText(radius, '5000');
      await tester.tap(find.text('Save settings'));
      await tester.pumpAndSettle();
      expect(find.text('Must be between 10 and 2000'), findsOneWidget);
      expect(sent, isEmpty);

      await tester.enterText(radius, '300');
      await tester.tap(find.text('Save settings'));
      await tester.pumpAndSettle();
      expect(sent.single, {'office.radius_m': 300});
    });

    testWidgets('server field errors appear under the field', (tester) async {
      _tall(tester);
      await tester.pumpWidget(app(
        Scaffold(
          body: SettingsForm(
            initial: const {'food.cutoff_time': '18:00'},
            onSave: (_) async => const ApiException(
              code: 'VALIDATION_ERROR',
              message: 'bad',
              details: [FieldError('food.cutoff_time', 'falls in a DST gap')],
            ),
          ),
        ),
        const [],
      ),);
      await tester.enterText(find.widgetWithText(TextFormField, 'Food cut-off time'), '19:00');
      await tester.tap(find.text('Save settings'));
      await tester.pumpAndSettle();
      expect(find.text('falls in a DST gap'), findsOneWidget);
    });
  });
}
