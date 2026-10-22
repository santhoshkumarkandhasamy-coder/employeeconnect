import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../profile/domain/profile_models.dart'
    show
        validateAddress,
        validateAnniversary,
        validateDob,
        validateEmail,
        validateEmergencyName,
        validateEmergencyPhone,
        validatePhone;
import '../data/admin_repository.dart';
import '../domain/admin_models.dart';
import '../domain/ops_models.dart';
import 'admin_providers.dart';
import 'admin_widgets.dart';
import 'employees_screen.dart';

/// Create (existing == null) or edit an employee. Pops `true` when saved.
class EmployeeFormScreen extends ConsumerStatefulWidget {
  const EmployeeFormScreen({super.key, this.existing});
  final Employee? existing;

  @override
  ConsumerState<EmployeeFormScreen> createState() => _EmployeeFormScreenState();
}

class _EmployeeFormScreenState extends ConsumerState<EmployeeFormScreen> {
  final _form = GlobalKey<FormState>();
  late final _id = TextEditingController(text: widget.existing?.employeeId ?? '');
  late final _name = TextEditingController(text: widget.existing?.fullName ?? '');
  late final _email = TextEditingController(text: widget.existing?.officialEmail ?? '');
  late final _phone = TextEditingController(text: _p?.phone ?? '');
  late final _join = TextEditingController(text: widget.existing?.joinDate ?? '');
  // Personal fields (edit only; shown when the detail carried `profile`).
  late final _dob = TextEditingController(text: _p?.dob ?? '');
  late final _anniv = TextEditingController(text: _p?.weddingAnniversary ?? '');
  late final _pEmail = TextEditingController(text: _p?.personalEmail ?? '');
  late final _address = TextEditingController(text: _p?.address ?? '');
  late final _ecName = TextEditingController(text: _p?.emergency?.name ?? '');
  late final _ecRel = TextEditingController(text: _p?.emergency?.relationship ?? '');
  late final _ecPhone = TextEditingController(text: _p?.emergency?.phone ?? '');
  late String _blood = _p?.bloodGroup ?? '';
  late String? _deptId = widget.existing?.department?.id;
  late String? _shiftId = widget.existing?.shift?.id;
  late String? _roleId = widget.existing?.role?.id;
  late EmployeeRef? _manager = widget.existing?.manager;
  final Map<String, String> _serverErrors = {};
  bool _saving = false;
  String? _formError;

  bool get _create => widget.existing == null;

  EmployeeProfile? get _p => widget.existing?.profile;

  /// Personal fields are editable only when the server sent `profile`
  /// (holders of `employee.view_sensitive`).
  bool get _showPersonal => !_create && _p != null;

  @override
  void dispose() {
    for (final c in [
      _id, _name, _email, _phone, _join, _dob, _anniv, _pEmail,
      _address, _ecName, _ecRel, _ecPhone,
    ]) {
      c.dispose();
    }
    super.dispose();
  }

  String? _srv(String key, String? local) => local ?? _serverErrors[key];

  Future<void> _pickDate() async {
    final now = DateTime.now();
    final initial = parseYmd(_join.text) ?? now;
    final d = await showDatePicker(
      context: context,
      initialDate: initial,
      firstDate: DateTime(now.year - 40),
      lastDate: DateTime(now.year + 2),
    );
    if (d != null) setState(() => _join.text = ymd(d));
  }

  Future<void> _pickManager() async {
    final picked = await Navigator.of(context).push<Employee>(
      MaterialPageRoute<Employee>(
        builder: (_) => EmployeesScreen(pickMode: true, excludeId: widget.existing?.id),
      ),
    );
    if (picked != null) {
      setState(() => _manager = EmployeeRef(
            id: picked.id,
            employeeId: picked.employeeId,
            fullName: picked.fullName,
          ),);
    }
  }

  /// The role picked in the form, from the role list when it is available.
  RoleItem? _selectedRole() {
    final list = ref.read(rolesProvider).valueOrNull;
    if (list == null || _roleId == null) return null;
    for (final r in list) {
      if (r.id == _roleId) return r;
    }
    return null;
  }

  Future<void> _save() async {
    setState(() {
      _serverErrors.clear();
      _formError = null;
    });
    if (!(_form.currentState?.validate() ?? false)) return;
    if (_deptId == null || _shiftId == null || _roleId == null) {
      setState(() => _formError = 'Department, shift and role are required.');
      return;
    }
    final body = employeeBody(
      create: _create,
      employeeId: _id.text,
      fullName: _name.text,
      email: _email.text,
      phone: _create ? _phone.text : '',
      personal: _showPersonal
          ? profilePatch(
              _p!,
              ProfileInput(
                dob: _dob.text,
                weddingAnniversary: _anniv.text,
                personalEmail: _pEmail.text,
                phone: _phone.text,
                emergencyName: _ecName.text,
                emergencyRelationship: _ecRel.text,
                emergencyPhone: _ecPhone.text,
                bloodGroup: _blood,
                address: _address.text,
              ),
            )
          : const {},
      departmentId: _deptId!,
      shiftId: _shiftId!,
      roleId: _roleId!,
      managerId: _manager?.id,
      joinDate: _join.text.trim(),
    );
    setState(() => _saving = true);
    final repo = ref.read(adminRepositoryProvider);
    // M1: creating or assigning an elevated role (role.assign / role.manage)
    // needs the actor's own password.
    final existingRole = widget.existing?.role;
    final roleList = ref.read(rolesProvider).valueOrNull;
    RoleItem? existingItem;
    if (existingRole != null && roleList != null) {
      for (final r in roleList) {
        if (r.id == existingRole.id) existingItem = r;
      }
    }
    // Editing an account that is already elevated also needs it; when the role
    // list is unavailable, any role other than plain `employee` counts.
    final needsReauth = employeeSaveNeedsReauth(selected: _selectedRole(), existing: existingItem) ||
        (!_create && roleList == null && existingRole != null && existingRole.name.toLowerCase() != 'employee');
    try {
      Future<void> send(String? currentPassword) async {
        if (_create) {
          final r = await repo.createEmployee(body, currentPassword: currentPassword);
          if (!mounted) return;
          await showTempPasswordDialog(
            context,
            employeeLabel: _name.text.trim(),
            password: r.temporaryPassword,
          );
        } else {
          await repo.updateEmployee(widget.existing!.id, body, currentPassword: currentPassword);
        }
      }

      if (needsReauth) {
        final done = await runWithReauth(
          context,
          reason: 'Giving an account this role needs your own password.',
          action: send,
        );
        if (!done) {
          if (mounted) setState(() => _saving = false);
          return;
        }
      } else {
        await send(null);
      }
      if (mounted) Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _saving = false;
        switch (e.code) {
          case 'EMPLOYEE_ID_EXISTS':
            _serverErrors['employee_id'] = adminErrorMessage(e);
          case 'EMAIL_EXISTS':
            _serverErrors['official_email'] = adminErrorMessage(e);
          case 'VALIDATION_ERROR':
            for (final d in e.details) {
              if (d.field.isNotEmpty) _serverErrors[d.field] = d.message;
            }
            _formError = adminErrorMessage(e);
          default:
            _formError = adminErrorMessage(e);
        }
      });
      _form.currentState?.validate();
    }
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    final needed = _create ? AdminPerm.employeeCreate : AdminPerm.employeeUpdate;
    final title = _create ? 'Add employee' : 'Edit employee';
    if (!perms.contains(needed)) return NoPermissionScreen(title: title);

    final depts = ref.watch(departmentRefsProvider);
    final shifts = ref.watch(shiftRefsProvider);
    final roles = ref.watch(rolesProvider);
    // Seats hint: only for holders of license.manage (the license is theirs to read).
    final license = _create && perms.contains(AdminPerm.licenseManage)
        ? ref.watch(licenseProvider).valueOrNull
        : null;
    final noSeats = license != null && license.isFull;

    List<({String id, String name})> opts(List<NamedRef> l) =>
        [for (final r in l) (id: r.id, name: r.name)];

    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: Text(title)),
        body: Form(
          key: _form,
          child: ListView(
            padding: const EdgeInsets.all(16),
            children: [
              TextFormField(
                controller: _id,
                enabled: _create,
                decoration: const InputDecoration(labelText: 'Employee ID', border: OutlineInputBorder()),
                textInputAction: TextInputAction.next,
                validator: (v) => _srv('employee_id', _create ? validateNewEmployeeId(v) : null),
              ),
              const SizedBox(height: 12),
              TextFormField(
                controller: _name,
                decoration: const InputDecoration(labelText: 'Full name', border: OutlineInputBorder()),
                textInputAction: TextInputAction.next,
                validator: (v) => _srv('full_name', validateFullName(v)),
              ),
              const SizedBox(height: 12),
              TextFormField(
                controller: _email,
                keyboardType: TextInputType.emailAddress,
                decoration: const InputDecoration(labelText: 'Official email', border: OutlineInputBorder()),
                textInputAction: TextInputAction.next,
                validator: (v) => _srv('official_email', validateOfficialEmail(v)),
              ),
              const SizedBox(height: 12),
              if (_create) ...[
                TextFormField(
                  controller: _phone,
                  keyboardType: TextInputType.phone,
                  decoration: const InputDecoration(labelText: 'Phone (optional)', border: OutlineInputBorder()),
                  textInputAction: TextInputAction.next,
                  validator: (v) => _srv('phone', validatePhone(v)),
                ),
                const SizedBox(height: 12),
              ],
              _refField('Department', depts.whenData(opts), _deptId, (v) => setState(() => _deptId = v)),
              const SizedBox(height: 12),
              _refField('Shift', shifts.whenData(opts), _shiftId, (v) => setState(() => _shiftId = v)),
              const SizedBox(height: 12),
              _refField(
                  'Role',
                  roles.whenData((l) {
                    final out = [
                      for (final r in assignableRoles(l, perms)) (id: r.id, name: r.name),
                    ];
                    // Keep the current role visible when editing (the server
                    // still decides whether the change is allowed).
                    final cur = widget.existing?.role;
                    if (cur != null && !out.any((o) => o.id == cur.id)) {
                      out.add((id: cur.id, name: cur.name));
                    }
                    return out;
                  }),
                  _roleId, (v) => setState(() => _roleId = v),
                  hint: 'Needs the role.view permission to list roles',),
              const SizedBox(height: 12),
              InputDecorator(
                decoration: const InputDecoration(labelText: 'Manager (optional)', border: OutlineInputBorder()),
                child: Row(
                  children: [
                    Expanded(child: Text(_manager?.display ?? 'No manager')),
                    TextButton(onPressed: _pickManager, child: const Text('Choose')),
                    if (_manager != null)
                      IconButton(
                        tooltip: 'Remove manager',
                        icon: const Icon(Icons.close),
                        onPressed: () => setState(() => _manager = null),
                      ),
                  ],
                ),
              ),
              const SizedBox(height: 12),
              TextFormField(
                controller: _join,
                readOnly: true,
                onTap: _pickDate,
                decoration: const InputDecoration(
                  labelText: 'Join date',
                  suffixIcon: Icon(Icons.calendar_today_outlined),
                  border: OutlineInputBorder(),
                ),
                validator: (v) => _srv('join_date', validateJoinDate(v)),
              ),
              if (_create)
                const Padding(
                  padding: EdgeInsets.only(top: 12),
                  child: Text(
                    'A temporary password is generated and shown once after you create the '
                    'account. The employee must change it at the first sign-in.',
                  ),
                ),
              if (_showPersonal) ..._personalSection(),
              if (_formError != null) ...[
                const SizedBox(height: 12),
                Text(_formError!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
              ],
              const SizedBox(height: 20),
              if (license != null) ...[
                Text(seatsHint(license), key: const ValueKey('seats-hint')),
                const SizedBox(height: 8),
              ],
              FilledButton(
                onPressed: (_saving || noSeats) ? null : _save,
                style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
                child: _saving
                    ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                    : Text(_create ? 'Create employee' : 'Save changes'),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _tf(String label, TextEditingController c, String key, String? Function(String?) v,
          {String? hint, TextInputType? type, int maxLines = 1,}) =>
      Padding(
        padding: const EdgeInsets.only(bottom: 12),
        child: TextFormField(
          controller: c,
          keyboardType: type,
          maxLines: maxLines,
          decoration: InputDecoration(labelText: label, hintText: hint, border: const OutlineInputBorder()),
          validator: (x) => _srv(key, v(x)),
        ),
      );

  /// Personal details. Only the fields you change are sent; clearing a field
  /// removes the stored value.
  List<Widget> _personalSection() => [
        const SizedBox(height: 8),
        Text('Personal details', style: Theme.of(context).textTheme.titleMedium),
        const Padding(
          padding: EdgeInsets.only(bottom: 12, top: 4),
          child: Text('Only changed fields are saved. Clearing a field removes it.'),
        ),
        _tf('Phone', _phone, 'phone', validatePhone, type: TextInputType.phone),
        _tf('Date of birth', _dob, 'dob', (v) => validateDob(v?.trim(), DateTime.now()),
            hint: 'YYYY-MM-DD',),
        _tf('Wedding anniversary', _anniv, 'wedding_anniversary',
            (v) => validateAnniversary(v?.trim(), DateTime.now()),
            hint: 'YYYY-MM-DD',),
        _tf('Personal email', _pEmail, 'personal_email', validateEmail,
            type: TextInputType.emailAddress,),
        Padding(
          padding: const EdgeInsets.only(bottom: 12),
          child: DropdownButtonFormField<String>(
            initialValue: bloodGroups.contains(_blood) ? _blood : '',
            isExpanded: true,
            decoration: const InputDecoration(labelText: 'Blood group', border: OutlineInputBorder()),
            items: [
              for (final b in bloodGroups)
                DropdownMenuItem(value: b, child: Text(b.isEmpty ? 'Not set' : b)),
            ],
            onChanged: (v) => setState(() => _blood = v ?? ''),
          ),
        ),
        _tf('Address', _address, 'address', validateAddress, maxLines: 3),
        _tf('Emergency contact name', _ecName, 'emergency_contact',
            (v) => validateEmergencyName(v, _ecPhone.text),),
        _tf('Emergency contact relationship', _ecRel, 'emergency_contact',
            (v) => (v ?? '').length > 50 ? 'At most 50 characters' : null,),
        _tf('Emergency contact phone', _ecPhone, 'emergency_contact',
            (v) => validateEmergencyPhone(_ecName.text, v), type: TextInputType.phone,),
      ];

  Widget _refField(
    String label,
    AsyncValue<List<({String id, String name})>> async,
    String? value,
    ValueChanged<String?> onChanged, {
    String? hint,
  }) {
    return async.when(
      loading: () => InputDecorator(
        decoration: InputDecoration(labelText: label, border: const OutlineInputBorder()),
        child: const LinearProgressIndicator(),
      ),
      error: (e, _) => InputDecorator(
        decoration: InputDecoration(
          labelText: label,
          border: const OutlineInputBorder(),
          errorText: '${adminMsg(e)}${hint == null ? '' : ' ($hint)'}',
        ),
        child: const Text('Not available'),
      ),
      data: (opts) => RefDropdown(label: label, options: opts, value: value, onChanged: onChanged),
    );
  }
}
