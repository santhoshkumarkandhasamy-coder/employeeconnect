import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../data/admin_repository.dart';
import '../domain/admin_models.dart';
import 'admin_providers.dart';
import 'admin_widgets.dart';

/// Inbox of forgot-password requests (`employee.reset_password`).
class ResetRequestsScreen extends ConsumerStatefulWidget {
  const ResetRequestsScreen({super.key});

  @override
  ConsumerState<ResetRequestsScreen> createState() => _ResetRequestsScreenState();
}

class _ResetRequestsScreenState extends ConsumerState<ResetRequestsScreen> {
  String _status = 'open';

  Future<void> _reset(ResetRequest r) async {
    final user = r.user;
    if (user == null) return;
    if (user.id == ref.read(adminSelfIdProvider)) {
      showAdminSnack(context, 'Use Change password in your profile to change your own password.');
      return;
    }
    final ok = await confirmAction(
      context,
      title: 'Reset password?',
      message: '${user.display} will be signed out of every device and must set a new password.',
      confirmLabel: 'Reset',
    );
    if (!ok || !mounted) return;
    try {
      // The target's role is not known here: try without, the server asks
      // for the password (422) only when the target holds permissions.
      final temp = await resetWithStepUp(
        context,
        needsPassword: false,
        call: (current) => ref
            .read(adminRepositoryProvider)
            .resetPassword(user.id, currentPassword: current),
      );
      if (temp == null) return;
      ref.invalidate(resetRequestsProvider(_status));
      if (!mounted) return;
      await showTempPasswordDialog(context, employeeLabel: user.display, password: temp);
    } on ApiException catch (e) {
      if (mounted) showAdminSnack(context, adminErrorMessage(e));
    }
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!perms.contains(AdminPerm.employeeResetPassword)) {
      return const NoPermissionScreen(title: 'Password reset requests');
    }
    final async = ref.watch(resetRequestsProvider(_status));
    final notifier = ref.read(resetRequestsProvider(_status).notifier);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Password reset requests')),
        body: AdminPagedBody<ResetRequest>(
          async: async,
          onRefresh: notifier.reload,
          onLoadMore: notifier.loadMore,
          emptyIcon: Icons.lock_reset,
          emptyMessage: 'No $_status requests.',
          header: Padding(
            padding: const EdgeInsets.all(12),
            child: Wrap(
              spacing: 8,
              children: [
                for (final s in const ['open', 'handled', 'expired'])
                  ChoiceChip(
                    label: Text('${s[0].toUpperCase()}${s.substring(1)}'),
                    selected: _status == s,
                    onSelected: (_) => setState(() => _status = s),
                  ),
              ],
            ),
          ),
          itemBuilder: (context, r) => Card(
            margin: const EdgeInsets.symmetric(vertical: 4),
            child: ListTile(
              title: Text(r.user?.display ?? 'Unknown user'),
              subtitle: Text(r.requestedAt == null
                  ? r.status
                  : 'Requested ${ymd(r.requestedAt!)} ${formatTime(r.requestedAt!)}',),
              trailing: r.status == 'open' && r.user != null
                  ? FilledButton(
                      onPressed: () => _reset(r),
                      style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
                      child: const Text('Reset'),
                    )
                  : null,
            ),
          ),
        ),
      ),
    );
  }
}
