import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/domain/admin_models.dart' show adminErrorMessage;
import '../../admin/presentation/admin_widgets.dart'
    show adminPermissionsProvider, confirmAction, showAdminSnack;
import '../data/device_repository.dart';
import '../domain/device_models.dart';
import 'device_form_screen.dart';
import 'device_job.dart';
import 'device_providers.dart';

/// One device: status, last error, identity (serial), sync history, and the
/// actions allowed for the caller's permissions.
class DeviceDetailScreen extends ConsumerStatefulWidget {
  const DeviceDetailScreen({required this.deviceId, super.key, this.wait});
  final String deviceId;
  final Future<void> Function(Duration)? wait;

  @override
  ConsumerState<DeviceDetailScreen> createState() => _DeviceDetailState();
}

class _DeviceDetailState extends ConsumerState<DeviceDetailScreen> {
  bool _busy = false;
  DeviceSync? _test;

  void _reload() => ref.invalidate(deviceDetailProvider(widget.deviceId));

  Future<void> _sync() async {
    setState(() => _busy = true);
    final repo = ref.read(deviceRepositoryProvider);
    final job = await runDeviceJob(context, repo, () => repo.syncNow(widget.deviceId), wait: widget.wait);
    if (!mounted) return;
    setState(() => _busy = false);
    if (job != null) {
      showAdminSnack(context, job.summary);
      _reload();
    }
  }

  Future<void> _testConnection() async {
    setState(() {
      _busy = true;
      _test = null;
    });
    final repo = ref.read(deviceRepositoryProvider);
    final job = await runDeviceJob(context, repo, () => repo.testDevice(widget.deviceId), wait: widget.wait);
    if (!mounted) return;
    setState(() {
      _busy = false;
      _test = job;
    });
  }

  Future<void> _acceptIdentity(Device d) async {
    final ok = await confirmAction(
      context,
      title: 'Accept the new machine?',
      message: 'Only do this if the machine "${d.name}" was replaced on purpose. The next sync will '
          'remember the new machine. Until then no punches from it are used.',
      confirmLabel: 'Accept',
    );
    if (!ok || !mounted) return;
    setState(() => _busy = true);
    try {
      await ref.read(deviceRepositoryProvider).acceptIdentity(d.id);
      if (!mounted) return;
      showAdminSnack(context, 'Accepted. Run Sync now to finish.');
      _reload();
    } on ApiException catch (e) {
      if (mounted) showAdminSnack(context, adminErrorMessage(e));
    }
    if (mounted) setState(() => _busy = false);
  }

  Future<void> _edit(Device d) async {
    final saved = await Navigator.of(context).push<bool>(
      MaterialPageRoute<bool>(builder: (_) => DeviceFormScreen(existing: d)),
    );
    if (saved ?? false) _reload();
  }

  Future<void> _delete(Device d) async {
    final ok = await confirmAction(
      context,
      title: 'Delete this device?',
      message: 'A device that already has punches cannot be deleted; disable it instead.',
      confirmLabel: 'Delete',
    );
    if (!ok || !mounted) return;
    try {
      await ref.read(deviceRepositoryProvider).deleteDevice(d.id);
      if (mounted) Navigator.of(context).pop();
    } on ApiException catch (e) {
      if (mounted) showAdminSnack(context, adminErrorMessage(e));
    }
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    final canManage = canManageDevices(perms);
    final canSync = canSyncDevices(perms);
    final canEdit = canManage && ref.watch(deviceEditAllowedProvider);
    final async = ref.watch(deviceDetailProvider(widget.deviceId));
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(
          title: Text(async.valueOrNull?.name ?? 'Device'),
          actions: [
            if (canEdit && async.hasValue)
              IconButton(
                key: const ValueKey('edit-device'),
                tooltip: 'Edit',
                onPressed: () => _edit(async.value!),
                icon: const Icon(Icons.edit_outlined),
              ),
          ],
        ),
        body: async.when(
          loading: () => const LoadingView(),
          error: (e, _) => ErrorView(message: deviceMsg(e), onRetry: _reload),
          data: (d) => RefreshIndicator(
            onRefresh: () async {
              _reload();
              await ref.read(deviceDetailProvider(widget.deviceId).future).then((_) {}, onError: (Object _) {});
            },
            child: ListView(
              physics: const AlwaysScrollableScrollPhysics(),
              padding: const EdgeInsets.all(16),
              children: [
                _StatusCard(device: d),
                const SizedBox(height: 8),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    if (canSync && d.enabled)
                      FilledButton.icon(
                        key: const ValueKey('detail-sync'),
                        onPressed: _busy ? null : _sync,
                        icon: const Icon(Icons.sync),
                        label: const Text('Sync now'),
                      ),
                    if (canManage)
                      OutlinedButton.icon(
                        key: const ValueKey('detail-test'),
                        onPressed: _busy ? null : _testConnection,
                        icon: const Icon(Icons.network_check),
                        label: const Text('Test connection'),
                      ),
                    if (canManage && d.lastErrorCode == 'DEVICE_IDENTITY_CHANGED')
                      FilledButton.tonalIcon(
                        key: const ValueKey('accept-identity'),
                        onPressed: _busy ? null : () => _acceptIdentity(d),
                        icon: const Icon(Icons.verified_outlined),
                        label: const Text('Accept new identity'),
                      ),
                    if (canEdit)
                      TextButton.icon(
                        onPressed: _busy ? null : () => _delete(d),
                        icon: const Icon(Icons.delete_outline),
                        label: const Text('Delete'),
                      ),
                  ],
                ),
                if (_busy)
                  const Padding(padding: EdgeInsets.only(top: 12), child: LinearProgressIndicator()),
                if (_test != null) ...[
                  const SizedBox(height: 12),
                  Card(child: Padding(padding: const EdgeInsets.all(12), child: TestResultView(job: _test!))),
                ],
                const SizedBox(height: 16),
                Text('Recent syncs', style: Theme.of(context).textTheme.titleMedium),
                if (d.recentSyncs.isEmpty)
                  const Padding(
                    padding: EdgeInsets.symmetric(vertical: 16),
                    child: Text('No syncs yet.'),
                  )
                else
                  for (final s in d.recentSyncs) _SyncTile(sync: s),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _StatusCard extends StatelessWidget {
  const _StatusCard({required this.device});
  final Device device;

  @override
  Widget build(BuildContext context) {
    final d = device;
    final theme = Theme.of(context);
    Widget row(String k, String v) => Padding(
          padding: const EdgeInsets.symmetric(vertical: 2),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              SizedBox(width: 140, child: Text(k, style: theme.textTheme.bodySmall)),
              Expanded(child: Text(v)),
            ],
          ),
        );
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(child: Text(deviceStatusLabel(d), style: theme.textTheme.titleLarge)),
                Text(d.transport.toUpperCase()),
              ],
            ),
            const SizedBox(height: 8),
            row('Address', d.address),
            if (d.locationLabel != null) row('Location', d.locationLabel!),
            row('Last sync', relativeTime(d.lastSyncAt)),
            row('Last success', relativeTime(d.lastSuccessAt)),
            row('Machine clock', clockOffsetText(d.lastClockOffsetS)),
            row('People / records', '${d.usersOnDevice ?? '-'} / ${d.recordsOnDevice ?? '-'}'),
            row('Unmatched users', '${d.unmatchedUsers}'),
            row('Serial number', d.serialNumber ?? 'not yet recorded'),
            row('Model', [d.model, d.firmware].whereType<String>().join('  ').isEmpty
                ? '-'
                : [d.model, d.firmware].whereType<String>().join('  '),),
            if (d.lastErrorCode != null && d.status != DeviceStatus.ok)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  deviceJobErrorText(d.lastErrorCode),
                  key: const ValueKey('last-error'),
                  style: TextStyle(color: theme.colorScheme.error),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _SyncTile extends StatelessWidget {
  const _SyncTile({required this.sync});
  final DeviceSync sync;

  @override
  Widget build(BuildContext context) {
    final s = sync;
    final when = s.finishedAt ?? s.queuedAt;
    final kind = switch (s.kind) {
      'schedule' => 'Scheduled',
      'manual' => 'Sync now',
      'reconcile' => 'Re-check',
      'test' => 'Test',
      _ => s.kind,
    };
    final icon = s.succeeded
        ? Icons.check_circle_outline
        : (s.isFinished ? Icons.error_outline : Icons.hourglass_empty);
    return ListTile(
      contentPadding: EdgeInsets.zero,
      dense: true,
      leading: Icon(icon, color: s.isFinished && !s.succeeded ? Theme.of(context).colorScheme.error : null),
      title: Text('$kind  -  ${relativeTime(when)}'),
      subtitle: Text(s.isFinished ? s.summary : 'In progress'),
    );
  }
}
