import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../data/admin_repository.dart';
import '../domain/admin_models.dart';
import '../domain/ops_models.dart';
import 'admin_providers.dart';
import 'admin_widgets.dart';
import 'employee_form_screen.dart';
import 'leave_balances_screen.dart';
import 'sessions_screen.dart';

/// Employee detail and actions (deactivate, unlock, reset password, delete,
/// leave balances). Sensitive: [SecureScreen].
class EmployeeDetailScreen extends ConsumerWidget {
  const EmployeeDetailScreen({required this.employeeId, super.key});
  final String employeeId;

  Future<void> _run(
    BuildContext context,
    WidgetRef ref,
    Future<void> Function() action, {
    required String done,
    bool pop = false,
  }) async {
    try {
      await action();
      ref.invalidate(employeeDetailProvider(employeeId));
      if (!context.mounted) return;
      showAdminSnack(context, done);
      if (pop) Navigator.of(context).pop();
    } on ApiException catch (e) {
      if (context.mounted) showAdminSnack(context, adminErrorMessage(e));
    }
  }

  Future<void> _resetPassword(
    BuildContext context,
    WidgetRef ref,
    Employee e,
    List<RoleItem>? roles,
  ) async {
    final ok = await confirmAction(
      context,
      title: 'Reset password?',
      message: '${e.fullName} will be signed out of every device and must set a new password '
          'at the next sign-in.',
      confirmLabel: 'Reset',
    );
    if (!ok || !context.mounted) return;
    try {
      final temp = await resetWithStepUp(
        context,
        needsPassword: targetHoldsPermissions(e, roles),
        call: (current) =>
            ref.read(adminRepositoryProvider).resetPassword(e.id, currentPassword: current),
      );
      if (temp == null) return;
      ref.invalidate(employeeDetailProvider(employeeId));
      if (!context.mounted) return;
      await showTempPasswordDialog(context, employeeLabel: e.fullName, password: temp);
    } on ApiException catch (err) {
      if (context.mounted) showAdminSnack(context, adminErrorMessage(err));
    }
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!perms.contains(AdminPerm.employeeView)) {
      return const NoPermissionScreen(title: 'Employee');
    }
    final selfId = ref.watch(adminSelfIdProvider);
    final async = ref.watch(employeeDetailProvider(employeeId));
    final repo = ref.read(adminRepositoryProvider);
    // Roles are only listed for holders of role.view; without them the role
    // name decides whether the target holds permissions.
    final roles = perms.contains(AdminPerm.roleView) ? ref.watch(rolesProvider).valueOrNull : null;
    final canAssign = perms.contains(AdminPerm.roleAssign);

    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Employee')),
        body: async.when(
          loading: () => const LoadingView(),
          error: (e, _) => ErrorView(
            message: adminMsg(e),
            onRetry: () => ref.invalidate(employeeDetailProvider(employeeId)),
          ),
          data: (e) {
            final isSelf = e.id == selfId;
            // H1: the server refuses these for targets holding permissions
            // unless the actor has role.assign; hide them instead of failing.
            final mayManageStatus = canAssign || !targetHoldsPermissions(e, roles);
            return ListView(
              padding: const EdgeInsets.all(16),
              children: [
                Text(e.fullName, style: Theme.of(context).textTheme.headlineSmall),
                const SizedBox(height: 4),
                Wrap(spacing: 8, children: [
                  Chip(label: Text(e.isActive ? 'Active' : 'Inactive')),
                  if (e.isLocked) const Chip(label: Text('Locked')),
                ],),
                const Divider(height: 24),
                _row('Employee ID', e.employeeId),
                _row('Email', e.officialEmail),
                _row('Department', e.department?.name ?? '-'),
                _row('Shift', e.shift?.name ?? '-'),
                _row('Role', e.role?.name ?? '-'),
                _row('Manager', e.manager?.display ?? '-'),
                _row('Joined', e.joinDate ?? '-'),
                if (e.hasAccountInfo)
                  _row('Last sign-in', e.lastLoginAt == null
                      ? 'Never'
                      : '${ymd(e.lastLoginAt!)} ${formatTime(e.lastLoginAt!)}',),
                if (!e.isActive && e.deactivatedAt != null)
                  _row(
                    'Deactivated',
                    '${ymd(e.deactivatedAt!)} (${DeactivationReason.labelFor(e.deactivationReason)})',
                  ),
                if (e.isLocked && e.lockedUntil != null)
                  _row('Locked until', '${ymd(e.lockedUntil!)} ${formatTime(e.lockedUntil!)}'),
                if (e.profile != null) ...[
                  const Divider(height: 24),
                  Text('Personal details', style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 4),
                  _row('Phone', e.profile!.phone ?? '-'),
                  _row('Date of birth', e.profile!.dob ?? '-'),
                  _row('Personal email', e.profile!.personalEmail ?? '-'),
                  _row('Blood group', e.profile!.bloodGroup ?? '-'),
                  _row('Address', e.profile!.address ?? '-'),
                  _row('Emergency', e.profile!.emergency == null
                      ? '-'
                      : '${e.profile!.emergency!.name} ${e.profile!.emergency!.phone}',),
                ],
                const SizedBox(height: 16),
                if (perms.contains(AdminPerm.employeeUpdate))
                  _action(Icons.edit_outlined, 'Edit details', () async {
                    final saved = await Navigator.of(context).push<bool>(
                      MaterialPageRoute<bool>(builder: (_) => EmployeeFormScreen(existing: e)),
                    );
                    if (saved == true) ref.invalidate(employeeDetailProvider(employeeId));
                  }),
                if (perms.contains(AdminPerm.leaveBalanceManage))
                  _action(Icons.beach_access_outlined, 'Leave balances', () {
                    Navigator.of(context).push<void>(
                      MaterialPageRoute<void>(
                        builder: (_) => LeaveBalancesScreen(employee: e),
                      ),
                    );
                  }),
                if (e.isLocked && mayManageStatus && perms.contains(AdminPerm.employeeUnlock))
                  _action(Icons.lock_open, 'Unlock account', () => _run(
                        context, ref, () => repo.unlockEmployee(e.id),
                        done: 'Account unlocked.',),),
                if (perms.contains(AdminPerm.sessionManage))
                  _action(Icons.devices_outlined, 'Sessions and devices', () {
                    Navigator.of(context).push<void>(
                      MaterialPageRoute<void>(
                        builder: (_) => SessionsScreen(userId: e.id, title: e.fullName),
                      ),
                    );
                  }),
                if (!isSelf && perms.contains(AdminPerm.employeeResetPassword))
                  _action(Icons.lock_reset, 'Reset password', () => _resetPassword(context, ref, e, roles)),
                if (!isSelf && mayManageStatus && perms.contains(AdminPerm.employeeDeactivate))
                  e.isActive
                      ? _action(Icons.person_off_outlined, 'Deactivate', () async {
                          final reason = await pickDeactivationReason(context, e.fullName);
                          if (reason != null && context.mounted) {
                            await _run(context, ref,
                                () => repo.setEmployeeActive(e.id, active: false, reason: reason),
                                done: 'Employee deactivated.',);
                          }
                        })
                      : _action(Icons.person_outline, 'Activate', () async {
                          final ok = await confirmAction(
                            context,
                            title: 'Activate ${e.fullName}?',
                            message: 'They will be able to sign in again with their existing '
                                'password, which they must change at the next sign-in, and with '
                                'the access of their role (${e.role?.name ?? 'unknown'}).',
                            confirmLabel: 'Activate',
                          );
                          if (ok && context.mounted) {
                            await _run(context, ref,
                                () => repo.setEmployeeActive(e.id, active: true),
                                done: 'Employee activated.',);
                          }
                        }),
                if (!isSelf && perms.contains(AdminPerm.employeeDelete))
                  _action(Icons.delete_outline, 'Delete employee', () async {
                    final ok = await confirmAction(
                      context,
                      title: 'Delete ${e.fullName}?',
                      message: 'The employee is deactivated and removed from lists. History is '
                          'kept and the employee ID can never be reused.',
                      confirmLabel: 'Delete',
                    );
                    if (ok && context.mounted) {
                      await _run(context, ref, () => repo.deleteEmployee(e.id),
                          done: 'Employee deleted.', pop: true,);
                    }
                  }, destructive: true,),
              ],
            );
          },
        ),
      ),
    );
  }

  Widget _row(String label, String value) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(width: 110, child: Text(label)),
            Expanded(child: Text(value)),
          ],
        ),
      );

  Widget _action(IconData icon, String label, VoidCallback onTap, {bool destructive = false}) =>
      Builder(
        builder: (context) => ListTile(
          leading: Icon(icon, color: destructive ? Theme.of(context).colorScheme.error : null),
          title: Text(label),
          onTap: onTap,
        ),
      );
}
