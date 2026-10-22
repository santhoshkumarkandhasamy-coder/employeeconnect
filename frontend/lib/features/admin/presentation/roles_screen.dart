import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/widgets/state_views.dart';
import '../data/admin_repository.dart';
import '../domain/admin_models.dart';
import 'admin_providers.dart';
import 'admin_widgets.dart';

/// Role list (`role.view`); editing needs `role.manage`.
class RolesScreen extends ConsumerWidget {
  const RolesScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!perms.contains(AdminPerm.roleView)) {
      return const NoPermissionScreen(title: 'Roles and permissions');
    }
    final async = ref.watch(rolesProvider);
    final canManage = perms.contains(AdminPerm.roleManage);

    Future<void> open(RoleItem? role) async {
      await Navigator.of(context).push<bool>(
        MaterialPageRoute<bool>(builder: (_) => RoleEditorScreen(role: role)),
      );
      ref.invalidate(rolesProvider);
    }

    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Roles and permissions')),
        floatingActionButton: canManage
            ? FloatingActionButton.extended(
                onPressed: () => open(null),
                icon: const Icon(Icons.add),
                label: const Text('New role'),
              )
            : null,
        body: async.when(
          loading: () => const LoadingView(),
          error: (e, _) => ErrorView(message: adminMsg(e), onRetry: () => ref.invalidate(rolesProvider)),
          data: (roles) => roles.isEmpty
              ? const EmptyView(icon: Icons.admin_panel_settings_outlined, message: 'No roles yet.')
              : RefreshIndicator(
                  onRefresh: () async {
                    ref.invalidate(rolesProvider);
                    await ref.read(rolesProvider.future).then((_) {}, onError: (Object _) {});
                  },
                  child: ListView(
                    physics: const AlwaysScrollableScrollPhysics(),
                    padding: const EdgeInsets.fromLTRB(12, 8, 12, 96),
                    children: [
                      for (final r in roles)
                        Card(
                          child: ListTile(
                            title: Text(r.name),
                            subtitle: r.description.isEmpty ? null : Text(r.description),
                            trailing: r.isSystem ? const Chip(label: Text('System')) : null,
                            onTap: () => open(r),
                          ),
                        ),
                    ],
                  ),
                ),
        ),
      ),
    );
  }
}

class RoleEditorScreen extends ConsumerStatefulWidget {
  const RoleEditorScreen({super.key, this.role});

  /// Null to create a custom role.
  final RoleItem? role;

  @override
  ConsumerState<RoleEditorScreen> createState() => _RoleEditorScreenState();
}

class _RoleEditorScreenState extends ConsumerState<RoleEditorScreen> {
  final _form = GlobalKey<FormState>();
  late final _name = TextEditingController(text: widget.role?.name ?? '');
  late final _desc = TextEditingController(text: widget.role?.description ?? '');
  Set<String>? _codes;
  Set<String> _original = {};
  bool _saving = false;
  String? _error;

  @override
  void dispose() {
    _name.dispose();
    _desc.dispose();
    super.dispose();
  }

  bool get _isNew => widget.role == null;

  Future<void> _save(RoleItem? role) async {
    if (!(_form.currentState?.validate() ?? false)) return;
    setState(() {
      _saving = true;
      _error = null;
    });
    final repo = ref.read(adminRepositoryProvider);
    try {
      final desc = _desc.text.trim().isEmpty ? null : _desc.text.trim();
      final codes = (_codes ?? <String>{}).toList()..sort();
      if (role == null) {
        await repo.createRole(_name.text.trim(), desc, codes);
      } else {
        // Permissions first: changing them needs the user's own password
        // (step-up re-auth, M1). Cancelling the prompt saves nothing.
        if (!_sameSet(_codes ?? <String>{}, _original)) {
          final done = await runWithReauth(
            context,
            reason: 'Changing the permissions of a role needs your own password.',
            action: (pw) => repo.setRolePermissions(role.id, codes, currentPassword: pw),
          );
          if (!done) {
            if (mounted) setState(() => _saving = false);
            return;
          }
          _original = {...codes};
        }
        if (!role.isSystem) await repo.updateRole(role.id, _name.text.trim(), desc);
      }
      if (mounted) Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      if (mounted) {
        setState(() {
          _saving = false;
          _error = adminErrorMessage(e);
        });
      }
    }
  }

  Future<void> _delete(RoleItem role) async {
    final ok = await confirmAction(
      context,
      title: 'Delete role "${role.name}"?',
      message: 'This is blocked while employees still have this role.',
      confirmLabel: 'Delete',
    );
    if (!ok || !mounted) return;
    try {
      await ref.read(adminRepositoryProvider).deleteRole(role.id);
      if (mounted) Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      if (mounted) showAdminSnack(context, adminErrorMessage(e));
    }
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!perms.contains(AdminPerm.roleView)) {
      return const NoPermissionScreen(title: 'Role');
    }
    final canManage = perms.contains(AdminPerm.roleManage);
    final catalogue = ref.watch(permissionCatalogueProvider);

    // `GET /admin/roles` already carries each role's permission_codes.
    final role = widget.role;
    if (_codes == null) {
      final base = role?.permissionCodes ?? const <String>[];
      _codes = {...base};
      _original = {...base};
    }

    final immutable = role?.isAdminRole ?? false;
    final editable = canManage && !immutable;

    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(
          title: Text(_isNew ? 'New role' : (role?.name ?? 'Role')),
          actions: [
            if (canManage && role != null && !role.isSystem)
              IconButton(
                tooltip: 'Delete role',
                icon: const Icon(Icons.delete_outline),
                onPressed: () => _delete(role),
              ),
          ],
        ),
        body: catalogue.when(
                loading: () => const LoadingView(),
                error: (e, _) => ErrorView(
                  message: adminMsg(e),
                  onRetry: () => ref.invalidate(permissionCatalogueProvider),
                ),
                data: (defs) => Form(
                  key: _form,
                  child: ListView(
                    padding: const EdgeInsets.all(16),
                    children: [
                      if (immutable)
                        const Padding(
                          padding: EdgeInsets.only(bottom: 12),
                          child: Text('The Admin role always has every permission and cannot be edited.'),
                        ),
                      TextFormField(
                        controller: _name,
                        enabled: editable && !(role?.isSystem ?? false),
                        decoration: const InputDecoration(labelText: 'Name', border: OutlineInputBorder()),
                        validator: validateRoleName,
                      ),
                      const SizedBox(height: 12),
                      TextFormField(
                        controller: _desc,
                        enabled: editable && !(role?.isSystem ?? false),
                        maxLines: 2,
                        decoration: const InputDecoration(labelText: 'Description', border: OutlineInputBorder()),
                        validator: (v) => (v ?? '').length > 200 ? 'At most 200 characters' : null,
                      ),
                      const SizedBox(height: 16),
                      Text('Permissions', style: Theme.of(context).textTheme.titleMedium),
                      for (final entry in groupByModule(defs).entries)
                        ExpansionTile(
                          title: Text(entry.key),
                          subtitle: Text(
                            '${entry.value.where((d) => _codes!.contains(d.code)).length} of ${entry.value.length}',
                          ),
                          children: [
                            for (final d in entry.value)
                              CheckboxListTile(
                                value: _codes!.contains(d.code),
                                title: Text(d.code),
                                subtitle: d.description.isEmpty ? null : Text(d.description),
                                onChanged: editable
                                    ? (v) => setState(() {
                                          if (v ?? false) {
                                            _codes!.add(d.code);
                                          } else {
                                            _codes!.remove(d.code);
                                          }
                                        })
                                    : null,
                              ),
                          ],
                        ),
                      if (_error != null) ...[
                        const SizedBox(height: 12),
                        Text(_error!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
                      ],
                      if (editable) ...[
                        const SizedBox(height: 16),
                        FilledButton(
                          onPressed: _saving ? null : () => _save(role),
                          style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
                          child: Text(_isNew ? 'Create role' : 'Save changes'),
                        ),
                      ],
                    ],
                  ),
                ),
              ),
      ),
    );
  }
}

bool _sameSet(Set<String> a, Set<String> b) => a.length == b.length && a.containsAll(b);

String? validateRoleName(String? v) {
  final s = v?.trim() ?? '';
  if (s.isEmpty) return 'Enter a role name';
  if (!RegExp(r'^[a-z][a-z0-9_]{1,29}$').hasMatch(s)) {
    return 'Use 2 to 30 lowercase letters, digits or underscores, starting with a letter';
  }
  return null;
}
