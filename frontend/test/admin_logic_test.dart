import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/admin/domain/admin_models.dart';
import 'package:employee_connect/features/admin/presentation/leave_balances_screen.dart'
    show validateQuota;
import 'package:employee_connect/features/profile/domain/profile_models.dart' show validatePhone;
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('permission gating', () {
    test('sections follow permissions only', () {
      expect(adminSections(const []), isEmpty);
      expect(canOpenAdmin(const ['food.view_all']), isFalse);
      // HR/Admin who post announcements also find them on the Admin screen (not only under More)
      expect(canOpenAdmin(const ['announcement.manage']), isTrue);
      expect(
        adminSections(const ['audit.view', 'employee.view', 'shift.manage']),
        [AdminSection.employees, AdminSection.shifts, AdminSection.audit],
      );
      expect(canOpenAdmin(const ['settings.manage']), isTrue);
    });
  });

  group('master fields', () {
    const name = FieldSpec('name', 'Name', FieldKind.text);
    test('text', () {
      expect(validateField(name, ' '), 'Enter name');
      expect(validateField(name, 'x' * 101), contains('at most 100'));
      expect(validateField(name, 'IT'), isNull);
    });

    test('integer and decimal ranges', () {
      const grace = FieldSpec('g', 'Grace', FieldKind.integer, min: 0, max: 240);
      expect(validateField(grace, 'abc'), 'Enter a whole number');
      expect(validateField(grace, '-1'), 'Must be at least 0');
      expect(validateField(grace, '241'), 'Must be at most 240');
      expect(validateField(grace, '15'), isNull);
      const quota = FieldSpec('q', 'Quota', FieldKind.decimal, min: 0, max: 366);
      expect(validateField(quota, '12.5'), isNull);
      expect(validateField(quota, 'NaN'), 'Enter a number');
      const optional = FieldSpec('o', 'Opt', FieldKind.integer, required: false, nullable: true);
      expect(validateField(optional, ''), isNull);
    });

    test('time and date', () {
      const t = FieldSpec('t', 'T', FieldKind.time);
      expect(validateField(t, '09:30'), isNull);
      expect(validateField(t, '24:00'), isNotNull);
      expect(validateField(t, '9:30'), isNotNull);
      const d = FieldSpec('d', 'D', FieldKind.date);
      expect(validateField(d, '2026-02-30'), isNotNull);
      expect(validateField(d, '2026-12-25'), isNull);
    });

    test('body for a shift matches the contract keys', () {
      final body = buildMasterBody(
        shiftSpec,
        {'name': ' Day ', 'start_time': '09:00', 'end_time': '18:00', 'grace_minutes': '10'},
        {'working_days': [1, 2, 3, 4, 5], 'is_active': true},
      );
      expect(body, {
        'name': 'Day',
        'start_time': '09:00',
        'end_time': '18:00',
        'grace_minutes': 10,
        'working_days': [1, 2, 3, 4, 5],
        'is_active': true,
      });
      expect(validateMasterCross(shiftSpec, body), isNull);
      expect(validateMasterCross(shiftSpec, {...body, 'working_days': <int>[]}), isNotNull);
      expect(validateMasterCross(shiftSpec, {...body, 'end_time': '09:00'}), isNotNull);
    });

    test('empty nullable leave-type limits are sent as null', () {
      final body = buildMasterBody(
        leaveTypeSpec,
        {
          'code': 'CL',
          'name': 'Casual',
          'annual_quota': '12',
          'requires_attachment_after_days': '',
          'max_consecutive_days': '5',
        },
        {'is_paid': true, 'allow_half_day': false, 'is_active': true},
      );
      expect(body['annual_quota'], 12.0);
      expect(body['requires_attachment_after_days'], isNull);
      expect(body['max_consecutive_days'], 5);
      expect(body['allow_half_day'], false);
    });
  });

  group('settings', () {
    SettingSpec spec(String key) => settingSpecs.firstWhere((s) => s.key == key);

    test('ranges mirror the contract', () {
      final radius = spec('office.radius_m');
      expect(validateSetting(radius, '9'), isNotNull);
      expect(validateSetting(radius, '2001'), isNotNull);
      expect(validateSetting(radius, '200'), isNull);
      expect(validateSetting(radius, '200.5'), 'Enter a whole number');
      expect(validateSetting(spec('office.lat'), '91'), isNotNull);
      expect(validateSetting(spec('office.lat'), '12.97'), isNull);
      expect(validateSetting(spec('food.cutoff_time'), '18:00'), isNull);
      expect(validateSetting(spec('food.cutoff_time'), '6pm'), isNotNull);
      expect(validateSetting(spec('permission.monthly_count_limit'), '0'), isNull);
      expect(validateSetting(spec('auth.inactivity_timeout_minutes'), '31'), isNotNull);
    });

    test('values are parsed by kind and only changes are sent', () {
      expect(parseSettingValue(spec('office.radius_m'), ' 300 '), 300);
      expect(parseSettingValue(spec('office.lat'), '12.5'), 12.5);
      expect(parseSettingValue(spec('food.cutoff_time'), '18:30'), '18:30');
      expect(settingText(200.0), '200');
      expect(settingText(12.97), '12.97');
      expect(
        changedSettings(
          {'office.radius_m': 200.0, 'food.cutoff_time': '18:00', 'leave.allow_cancel_approved': false},
          {'office.radius_m': 200, 'food.cutoff_time': '18:30', 'leave.allow_cancel_approved': false},
        ),
        {'food.cutoff_time': '18:30'},
      );
    });
  });

  group('employee form', () {
    test('validators', () {
      expect(validateNewEmployeeId(''), isNotNull);
      expect(validateNewEmployeeId('EMP 1'), isNotNull);
      expect(validateNewEmployeeId('EMP042'), isNull);
      expect(validateOfficialEmail('a@corp'), isNotNull);
      expect(validateOfficialEmail('a@corp.com'), isNull);
      expect(validatePhone(''), isNull);
      expect(validatePhone('abc'), isNotNull);
      expect(validatePhone('+91 98765 43210'), isNull);
      expect(validateJoinDate('2026-13-01'), isNotNull);
      expect(validateFullName('  '), isNotNull);
    });

    test('create body has the contract keys; update omits create-only keys', () {
      final create = employeeBody(
        create: true,
        employeeId: ' EMP042 ',
        fullName: 'A B',
        email: 'a@corp.com',
        phone: '',
        departmentId: 'd',
        shiftId: 's',
        roleId: 'r',
        managerId: null,
        joinDate: '2026-10-01',
      );
      expect(create['employee_id'], 'EMP042');
      expect(create.containsKey('initial_password'), isFalse);
      expect(create.containsKey('manager_id'), isTrue);
      final update = employeeBody(
        create: false,
        fullName: 'A B',
        email: 'a@corp.com',
        phone: '',
        departmentId: 'd',
        shiftId: 's',
        roleId: 'r',
        joinDate: '2026-10-01',
      );
      expect(update.containsKey('employee_id'), isFalse);
      expect(update.containsKey('initial_password'), isFalse);
      expect(update.containsKey('phone'), isFalse);
    });

    test('quota validation', () {
      expect(validateQuota('3', 4), contains('below'));
      expect(validateQuota('x', 0), 'Enter a number');
      expect(validateQuota('12', 4), isNull);
    });
  });

  group('privilege helpers', () {
    const hr = RoleItem(id: 'hr', name: 'hr', permissionCodes: ['employee.view', 'employee.create']);
    const emp = RoleItem(id: 'e', name: 'employee', permissionCodes: []);
    const admin = RoleItem(
      id: 'a',
      name: 'admin',
      permissionCodes: ['employee.view', 'employee.create', 'role.assign', 'role.manage'],
    );
    const custom = RoleItem(id: 'c', name: 'ops', permissionCodes: ['role.manage']);

    test('elevated roles hold role.assign or role.manage', () {
      expect(isElevatedRole(admin), isTrue);
      expect(isElevatedRole(custom), isTrue);
      expect(isElevatedRole(hr), isFalse);
      expect(isElevatedRole(emp), isFalse);
      expect(isElevatedRole(null), isFalse);
    });

    test('saving needs re-auth for a new elevated role or an already elevated account', () {
      expect(employeeSaveNeedsReauth(selected: emp, existing: emp), isFalse);
      expect(employeeSaveNeedsReauth(selected: admin, existing: emp), isTrue);
      expect(employeeSaveNeedsReauth(selected: emp, existing: admin), isTrue);
      expect(employeeSaveNeedsReauth(selected: custom, existing: custom), isTrue);
      expect(employeeSaveNeedsReauth(selected: hr, existing: hr), isFalse);
    });

    test('role dropdown only offers assignable roles', () {
      const all = [admin, hr, emp, custom];
      expect(assignableRoles(all, const ['employee.create']).map((r) => r.id), ['e']);
      expect(
        assignableRoles(all, const ['employee.view', 'employee.create', 'role.assign']).map((r) => r.id),
        ['hr', 'e'],
      );
      expect(
        assignableRoles(all, admin.permissionCodes!).map((r) => r.id),
        ['a', 'hr', 'e', 'c'],
      );
    });

    test('targets with permissions are detected from the role list or its name', () {
      Employee target(String roleId, String roleName) => Employee(
            id: 'x',
            employeeId: 'E',
            fullName: 'N',
            role: NamedRef(id: roleId, name: roleName),
          );
      expect(targetHoldsPermissions(target('hr', 'hr'), const [hr, emp]), isTrue);
      expect(targetHoldsPermissions(target('e', 'employee'), const [hr, emp]), isFalse);
      expect(targetHoldsPermissions(target('q', 'employee'), null), isFalse);
      expect(targetHoldsPermissions(target('q', 'manager'), null), isTrue);
    });
  });

  group('parsing', () {
    test('reduced employee shape for managers is tolerated', () {
      final e = Employee.fromJson({
        'id': 'u',
        'employee_id': 'EMP009',
        'full_name': 'Report',
        'status': 'active',
      });
      expect(e.hasAccountInfo, isFalse);
      expect(e.isLocked, isFalse);
      expect(e.lastLoginAt, isNull);
      expect(e.lockedUntil, isNull);
    });

    test('profile with a string blood group', () {
      final e = Employee.fromJson({
        'id': 'u',
        'employee_id': 'E',
        'full_name': 'N',
        'profile': {'blood_group': 'AB-', 'phone': '+911234567890'},
      });
      expect(e.profile!.bloodGroup, 'AB-');
      expect(
        profilePatch(e.profile!, const ProfileInput(bloodGroup: 'AB-', phone: '+911234567890')),
        isEmpty,
      );
    });

    test('employee object per contract', () {
      final e = Employee.fromJson({
        'id': 'u',
        'employee_id': 'EMP001',
        'full_name': 'Asha',
        'official_email': 'a@corp.com',
        'department': {'id': 'd', 'name': 'IT'},
        'shift': {'id': 's', 'name': 'Day'},
        'role': {'id': 'r', 'name': 'hr'},
        'manager': {'id': 'm', 'employee_id': 'EMP002', 'full_name': 'Ravi'},
        'status': 'inactive',
        'is_locked': true,
        'locked_until': '2026-10-22T10:00:00Z',
        'join_date': '2026-01-01',
        'last_login_at': null,
      });
      expect(e.isActive, isFalse);
      expect(e.isLocked, isTrue);
      expect(e.manager!.display, 'Ravi (EMP002)');
      expect(e.department!.name, 'IT');
      expect(e.profile, isNull); // no employee.view_sensitive
    });

    test('detail profile is parsed when present', () {
      final e = Employee.fromJson({
        'id': 'u',
        'employee_id': 'EMP001',
        'full_name': 'Asha',
        'profile': {
          'dob': '1990-05-01',
          'phone': '+919876543210',
          'blood_group': 'O+',
          'emergency_contact': {'name': 'Ravi', 'relationship': 'Brother', 'phone': '+911234567890'},
          'personal_email': null,
        },
      });
      final p = e.profile!;
      expect(p.dob, '1990-05-01');
      expect(p.emergency!.name, 'Ravi');
      expect(p.personalEmail, isNull);
    });

    test('profilePatch sends only changed keys and null to clear', () {
      const orig = EmployeeProfile(
        dob: '1990-05-01',
        phone: '+919876543210',
        bloodGroup: 'O+',
        address: 'Old street',
      );
      // Nothing changed (phone typed with spaces normalises to the same value).
      expect(
        profilePatch(
          orig,
          const ProfileInput(
            dob: '1990-05-01',
            phone: '+91 98765 43210',
            bloodGroup: 'O+',
            address: 'Old street',
          ),
        ),
        isEmpty,
      );
      final patch = profilePatch(
        orig,
        const ProfileInput(
          dob: '1991-01-01',
          phone: '',
          bloodGroup: 'O+',
          address: '',
          personalEmail: 'me@x.com',
          emergencyName: 'Ravi',
          emergencyPhone: '+911234567890',
        ),
      );
      expect(patch['dob'], '1991-01-01');
      expect(patch['phone'], isNull);
      expect(patch.containsKey('phone'), isTrue);
      expect(patch['address'], isNull);
      expect(patch.containsKey('address'), isTrue);
      expect(patch['personal_email'], 'me@x.com');
      expect(patch.containsKey('blood_group'), isFalse);
      expect(patch['emergency_contact'], {'name': 'Ravi', 'phone': '+911234567890'});
    });

    test('update body never carries phone unless it is in the personal patch', () {
      final body = employeeBody(
        create: false,
        fullName: 'A',
        email: 'a@corp.com',
        departmentId: 'd',
        shiftId: 's',
        roleId: 'r',
        joinDate: '2026-10-01',
        personal: const {'phone': null},
      );
      expect(body.containsKey('phone'), isTrue);
      expect(body['phone'], isNull);
    });

    test('holidays are edited with PATCH, masters with PUT', () {
      expect(holidaySpec.updateMethod, 'PATCH');
      expect(shiftSpec.updateMethod, 'PUT');
      expect(leaveTypeSpec.hasApplyDefaults, isTrue);
    });

    test('server field limits', () {
      final code = departmentSpec.fields.firstWhere((f) => f.key == 'code');
      expect(validateField(code, 'IT 1'), isNotNull);
      expect(validateField(code, 'IT-1'), isNull);
      final quota = leaveTypeSpec.fields.firstWhere((f) => f.key == 'annual_quota');
      expect(validateField(quota, '12.25'), 'At most one decimal place');
      expect(validateField(quota, '1000'), isNotNull);
      final grace = shiftSpec.fields.firstWhere((f) => f.key == 'grace_minutes');
      expect(validateField(grace, '121'), isNotNull);
    });

    test('re-auth and expired temporary password messages', () {
      expect(
        adminErrorMessage(const ApiException(code: 'REAUTH_FAILED', message: 'raw')),
        contains('password'),
      );
      expect(
        adminErrorMessage(const ApiException(code: 'TEMP_PASSWORD_EXPIRED', message: 'raw')),
        contains('HR or Admin'),
      );
    });

    test('403 and 429 messages are friendly', () {
      expect(
        adminErrorMessage(const ApiException(code: 'FORBIDDEN', message: '')),
        contains('Admin role cannot be changed'),
      );
      expect(
        adminErrorMessage(const ApiException(code: 'FORBIDDEN', message: 'Cannot change the admin role')),
        'Cannot change the admin role',
      );
      expect(
        adminErrorMessage(const ApiException(
          code: 'RATE_LIMITED',
          message: 'x',
          retryAfter: Duration(seconds: 30),
        ),),
        contains('30 seconds'),
      );
    });

    test('leave balance rows accept nested or flat leave types', () {
      final a = LeaveBalanceRow.fromJson({
        'leave_type': {'id': 'x', 'code': 'CL', 'name': 'Casual'},
        'quota': 12,
        'used': 2.5,
        'pending': 1,
      });
      expect(a.label, 'Casual');
      expect(a.minQuota, 3.5);
      final b = LeaveBalanceRow.fromJson({'leave_type_id': 'y', 'leave_type_name': 'Sick', 'quota': 6});
      expect(b.leaveTypeId, 'y');
      expect(b.withQuota(8).quota, 8);
    });

    test('roles and permission grouping', () {
      final r = RoleItem.fromJson({
        'id': '1',
        'name': 'Admin',
        'is_system': true,
        'permission_codes': ['a.b', 'c.d'],
      });
      expect(r.isAdminRole, isTrue);
      expect(r.permissionCodes, ['a.b', 'c.d']);
      expect(RoleItem.fromJson({'id': '2', 'name': 'x'}).permissionCodes, isNull);
      final g = groupByModule([
        const PermissionDef(code: 'leave.view', module: 'leave'),
        const PermissionDef(code: 'employee.view', module: 'employee'),
        const PermissionDef(code: 'employee.create', module: 'employee'),
      ]);
      expect(g.keys.toList(), ['employee', 'leave']);
      expect(g['employee']!.map((d) => d.code), ['employee.create', 'employee.view']);
    });

    test('audit entries and json view', () {
      final a = AuditEntry.fromJson({
        'id': '1',
        'ts': '2026-10-22T10:00:00Z',
        'actor': {'id': 'u', 'employee_id': 'EMP001', 'full_name': 'Asha'},
        'action': 'employee.create',
        'entity_type': 'user',
        'before': null,
        'after': {'password': '***'},
        'outcome': 'success',
      });
      expect(a.actor!.employeeId, 'EMP001');
      expect(prettyJson(a.before), 'none');
      expect(prettyJson(a.after), contains('"password": "***"'));
    });
  });

  group('queries and errors', () {
    test('audit query', () {
      expect(const AuditQuery().isEmpty, isTrue);
      const q = AuditQuery(action: 'x', from: '2026-10-01');
      expect(q.toQuery(), {'action': 'x', 'from': '2026-10-01'});
      expect(q, const AuditQuery(action: 'x', from: '2026-10-01'));
      expect(validateAuditRange('2026-10-05', '2026-10-01'), isNotNull);
      expect(validateAuditRange('2026-10-01', null), isNull);
    });

    test('employee query drops empty filters', () {
      expect(const EmployeeQuery(q: ' ').toQuery(), isEmpty);
      expect(
        const EmployeeQuery(q: 'ash', departmentId: 'd', status: 'active').toQuery(),
        {'q': 'ash', 'department_id': 'd', 'status': 'active'},
      );
    });

    test('mergeById de-duplicates', () {
      expect(mergeById<String>(['a', 'b'], ['b', 'c'], (s) => s), ['a', 'b', 'c']);
    });

    test('domain error codes have friendly messages', () {
      String m(String code) => adminErrorMessage(ApiException(code: code, message: 'raw'));
      expect(m('LAST_ADMIN'), contains('last active admin'));
      expect(m('IN_USE'), contains('Deactivate'));
      expect(m('EMAIL_EXISTS'), contains('email'));
      expect(m('QUOTA_BELOW_USED'), contains('quota'));
      expect(m('SOMETHING'), 'raw');
      expect(
        adminErrorMessage(const ApiException(
          code: 'VALIDATION_ERROR',
          message: 'bad',
          details: [FieldError('office.radius_m', 'too big')],
        ),),
        'office.radius_m: too big',
      );
    });
  });
}
