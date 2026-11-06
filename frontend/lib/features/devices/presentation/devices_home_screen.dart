import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/domain/admin_models.dart' show adminErrorMessage;
import '../../admin/presentation/admin_widgets.dart'
    show NoPermissionScreen, adminPermissionsProvider, showAdminSnack;
import '../data/device_repository.dart';
import '../domain/device_models.dart';
import 'device_detail_screen.dart';
import 'device_form_screen.dart';
import 'device_job.dart';
import 'device_providers.dart';
import 'device_users_tab.dart';
import 'punch_log_tab.dart';

/// Attendance devices: Devices | Unmatched users | Punch log. Needs `device.view`;
/// map / sync / manage actions need their own permissions (the server checks
/// them again). Add and edit appear only in the web console.
class DevicesHomeScreen extends ConsumerWidget {
  const DevicesHomeScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!canViewDevices(perms)) return const NoPermissionScreen(title: 'Attendance devices');
    return SecureScreen(
      child: DefaultTabController(
        length: 3,
        child: Scaffold(
          appBar: AppBar(
            title: const Text('Attendance devices'),
            bottom: const TabBar(
              tabs: [
                Tab(text: 'Devices'),
                Tab(text: 'Unmatched users'),
                Tab(text: 'Punch log'),
              ],
            ),
          ),
          body: const TabBarView(children: [DevicesTab(), DeviceUsersTab(), PunchLogTab()]),
        ),
      ),
    );
  }
}

class DevicesTab extends ConsumerStatefulWidget {
  const DevicesTab({super.key, this.wait});

  /// Overridable pause between job polls (tests).
  final Future<void> Function(Duration)? wait;

  @override
  ConsumerState<DevicesTab> createState() => _DevicesTabState();
}

class _DevicesTabState extends ConsumerState<DevicesTab> {
  final Set<String> _busy = {};
  bool _busyAll = false;

  Future<void> _sync(Device d) async {
    setState(() => _busy.add(d.id));
    final repo = ref.read(deviceRepositoryProvider);
    final job = await runDeviceJob(context, repo, () => repo.syncNow(d.id), wait: widget.wait);
    if (!mounted) return;
    setState(() => _busy.remove(d.id));
    if (job != null) {
      showAdminSnack(context, '${d.name}: ${job.summary}');
      ref.invalidate(devicesProvider);
    }
  }

  Future<void> _syncAll() async {
    setState(() => _busyAll = true);
    String message;
    try {
      final jobs = await ref.read(deviceRepositoryProvider).syncAll();
      message = jobs.isEmpty
          ? 'There are no enabled devices to sync.'
          : 'Sync started on ${jobs.length} device${jobs.length == 1 ? '' : 's'}. Pull down to refresh in a moment.';
    } on ApiException catch (e) {
      message = adminErrorMessage(e);
    }
    if (!mounted) return;
    setState(() => _busyAll = false);
    showAdminSnack(context, message);
    ref.invalidate(devicesProvider);
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    final canSync = canSyncDevices(perms);
    final canAdd = canManageDevices(perms) && ref.watch(deviceEditAllowedProvider);
    final async = ref.watch(devicesProvider);
    return async.when(
      loading: () => const LoadingView(),
      error: (e, _) => ErrorView(message: deviceMsg(e), onRetry: () => ref.invalidate(devicesProvider)),
      data: (devices) => RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(devicesProvider);
          await ref.read(devicesProvider.future).then((_) {}, onError: (Object _) {});
        },
        child: ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.all(12),
          children: [
            if (canSync || canAdd)
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  if (canAdd)
                    FilledButton.icon(
                      key: const ValueKey('add-device'),
                      onPressed: () async {
                        final saved = await Navigator.of(context).push<bool>(
                          MaterialPageRoute<bool>(builder: (_) => const DeviceFormScreen()),
                        );
                        if (saved ?? false) ref.invalidate(devicesProvider);
                      },
                      icon: const Icon(Icons.add),
                      label: const Text('Add device'),
                    ),
                  if (canSync && devices.any((d) => d.enabled))
                    OutlinedButton.icon(
                      key: const ValueKey('sync-all'),
                      onPressed: _busyAll ? null : () => _syncAll(),
                      icon: const Icon(Icons.sync),
                      label: Text(_busyAll ? 'Starting' : 'Sync all'),
                    ),
                ],
              ),
            if (devices.isEmpty)
              const Padding(
                padding: EdgeInsets.only(top: 64),
                child: EmptyView(icon: Icons.router_outlined, message: 'No attendance devices have been added yet.'),
              ),
            for (final d in devices)
              DeviceCard(
                device: d,
                busy: _busy.contains(d.id),
                onSync: canSync && d.enabled ? () => _sync(d) : null,
                onOpen: () async {
                  await Navigator.of(context).push<void>(
                    MaterialPageRoute<void>(builder: (_) => DeviceDetailScreen(deviceId: d.id)),
                  );
                  ref.invalidate(devicesProvider);
                },
              ),
          ],
        ),
      ),
    );
  }
}

Color statusColor(BuildContext context, Device d) {
  final s = Theme.of(context).colorScheme;
  if (d.status == DeviceStatus.failing || d.status == DeviceStatus.offline) return s.error;
  if (d.status == DeviceStatus.ok && !d.clockDrift) return s.primary;
  return s.tertiary;
}

class DeviceCard extends StatelessWidget {
  const DeviceCard({required this.device, required this.onOpen, super.key, this.onSync, this.busy = false});
  final Device device;
  final VoidCallback onOpen;
  final VoidCallback? onSync;
  final bool busy;

  @override
  Widget build(BuildContext context) {
    final d = device;
    final theme = Theme.of(context);
    final color = statusColor(context, d);
    return Card(
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        onTap: onOpen,
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(child: Text(d.name, style: theme.textTheme.titleMedium)),
                  Chip(
                    key: ValueKey('status-${d.id}'),
                    label: Text(deviceStatusLabel(d)),
                    side: BorderSide(color: color),
                    visualDensity: VisualDensity.compact,
                  ),
                ],
              ),
              Text([if (d.locationLabel != null) d.locationLabel!, d.address].join('  -  '),
                  style: theme.textTheme.bodySmall,),
              const SizedBox(height: 4),
              Text('Last sync ${relativeTime(d.lastSyncAt)}  -  clock ${clockOffsetText(d.lastClockOffsetS)}'),
              if (d.unmatchedUsers > 0)
                Text('${d.unmatchedUsers} unmatched user${d.unmatchedUsers == 1 ? '' : 's'}',
                    style: TextStyle(color: theme.colorScheme.tertiary),),
              if (d.lastErrorCode != null && d.status != DeviceStatus.ok && d.status != DeviceStatus.disabled)
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Text(
                    deviceJobErrorText(d.lastErrorCode),
                    style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.error),
                  ),
                ),
              if (onSync != null)
                Align(
                  alignment: Alignment.centerRight,
                  child: TextButton.icon(
                    key: ValueKey('sync-${d.id}'),
                    onPressed: busy ? null : onSync,
                    icon: busy
                        ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                        : const Icon(Icons.sync),
                    label: Text(busy ? 'Syncing' : 'Sync now'),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
