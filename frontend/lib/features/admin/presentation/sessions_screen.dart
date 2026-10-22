import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../data/admin_repository.dart';
import '../domain/admin_models.dart';
import '../domain/ops_models.dart';
import 'admin_providers.dart';
import 'admin_widgets.dart';

String _stamp(DateTime? t) => t == null ? '-' : '${ymd(t)} ${formatTime(t)}';

/// Live sessions. With [userId] only that employee's sessions (and a "Sign
/// out all devices" action); otherwise everyone's (`session.manage`).
class SessionsScreen extends ConsumerWidget {
  const SessionsScreen({super.key, this.userId, this.title = 'Sessions and devices'});
  final String? userId;
  final String title;

  Future<void> _revoke(BuildContext context, WidgetRef ref, SessionItem s, SessionKey key) async {
    final ok = await confirmAction(
      context,
      title: 'Sign out this device?',
      message: '${s.user?.display ?? 'The user'} will be signed out of ${s.deviceLabel}. '
          'Other devices stay signed in.',
      confirmLabel: 'Sign out',
    );
    if (!ok || !context.mounted) return;
    try {
      await ref.read(adminRepositoryProvider).revokeSession(s.id);
      ref.invalidate(sessionsProvider(key));
      if (context.mounted) showAdminSnack(context, 'Session ended.');
    } on ApiException catch (e) {
      if (context.mounted) showAdminSnack(context, adminErrorMessage(e));
    }
  }

  Future<void> _revokeAll(BuildContext context, WidgetRef ref, SessionKey key) async {
    final ok = await confirmAction(
      context,
      title: 'Sign out all devices?',
      message: 'The employee is signed out everywhere but the account stays active.',
      confirmLabel: 'Sign out all',
    );
    if (!ok || !context.mounted) return;
    try {
      final n = await ref.read(adminRepositoryProvider).revokeAllSessions(userId!);
      ref.invalidate(sessionsProvider(key));
      if (context.mounted) showAdminSnack(context, 'Signed out of $n session(s).');
    } on ApiException catch (e) {
      if (context.mounted) showAdminSnack(context, adminErrorMessage(e));
    }
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!perms.contains(AdminPerm.sessionManage)) {
      return NoPermissionScreen(title: title);
    }
    final SessionKey key = (userId, null, null);
    final async = ref.watch(sessionsProvider(key));
    final notifier = ref.read(sessionsProvider(key).notifier);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(
          title: Text(title),
          actions: [
            if (userId != null)
              IconButton(
                tooltip: 'Sign out all devices',
                icon: const Icon(Icons.devices_other),
                onPressed: () => _revokeAll(context, ref, key),
              ),
          ],
        ),
        body: AdminPagedBody<SessionItem>(
          async: async,
          onRefresh: notifier.reload,
          onLoadMore: notifier.loadMore,
          emptyMessage: 'No active sessions.',
          emptyIcon: Icons.devices_outlined,
          itemBuilder: (context, s) => Card(
            child: ListTile(
              leading: Icon(s.isWeb ? Icons.computer : Icons.smartphone),
              title: Text(userId == null ? (s.user?.display ?? '-') : s.deviceLabel),
              subtitle: Text(
                '${userId == null ? '${s.deviceLabel}\n' : ''}'
                'Last active ${_stamp(s.lastSeenAt)}'
                '${s.ip == null ? '' : ' - ${s.ip}'}',
              ),
              isThreeLine: userId == null,
              trailing: IconButton(
                tooltip: 'Sign out this device',
                icon: const Icon(Icons.logout),
                onPressed: () => _revoke(context, ref, s, key),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
