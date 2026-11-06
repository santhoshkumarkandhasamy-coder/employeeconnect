import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../admin/domain/admin_models.dart' show Employee, adminErrorMessage;
import '../../admin/presentation/admin_widgets.dart'
    show AdminPagedBody, adminPermissionsProvider, confirmAction, showAdminSnack;
import '../../admin/presentation/employee_picker.dart';
import '../data/device_repository.dart';
import '../domain/device_models.dart';
import 'device_providers.dart';

/// Device users report: who punched on a machine and is (not) linked to an
/// employee. Mapping needs `device.map`.
class DeviceUsersTab extends ConsumerStatefulWidget {
  const DeviceUsersTab({super.key, this.pickEmployeeFn});

  /// Overridable employee chooser (tests).
  final Future<Employee?> Function(BuildContext)? pickEmployeeFn;

  @override
  ConsumerState<DeviceUsersTab> createState() => _DeviceUsersTabState();
}

class _DeviceUsersTabState extends ConsumerState<DeviceUsersTab> {
  String _status = 'unmatched';
  String? _deviceId;
  String _q = '';
  bool _busy = false;

  DeviceUserQuery get _key => DeviceUserQuery(status: _status, deviceId: _deviceId, q: _q);

  Future<void> _apply(Future<MappingResult> Function() call, String okMessage) async {
    setState(() => _busy = true);
    try {
      final r = await call();
      if (!mounted) return;
      showAdminSnack(
        context,
        r.recomputeQueued ? '$okMessage Attendance is being updated.' : okMessage,
      );
      ref.invalidate(deviceUsersProvider(_key));
      ref.invalidate(devicesProvider);
    } on ApiException catch (e) {
      if (mounted) showAdminSnack(context, adminErrorMessage(e));
    }
    if (mounted) setState(() => _busy = false);
  }

  Future<void> _map(DeviceUser u) async {
    final employee = await (widget.pickEmployeeFn ?? (c) => pickEmployee(c, title: 'Link to employee'))(context);
    if (employee == null || !mounted) return;
    var allDevices = true;
    final go = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setLocal) => AlertDialog(
          title: const Text('Link to employee'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('${u.name ?? 'Device user'} (number ${u.deviceUserId}) will be linked to '
                  '${employee.fullName.isEmpty ? employee.employeeId : employee.fullName}.'),
              CheckboxListTile(
                key: const ValueKey('all-devices'),
                contentPadding: EdgeInsets.zero,
                value: allDevices,
                onChanged: (v) => setLocal(() => allDevices = v ?? true),
                title: const Text('Do the same on all devices'),
                controlAffinity: ListTileControlAffinity.leading,
              ),
            ],
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
            FilledButton(
              key: const ValueKey('confirm-map'),
              onPressed: () => Navigator.pop(ctx, true),
              child: const Text('Link'),
            ),
          ],
        ),
      ),
    );
    if (!(go ?? false) || !mounted) return;
    await _apply(
      () => ref.read(deviceRepositoryProvider).mapUser(u.id, userId: employee.id, allDevices: allDevices),
      'Linked.',
    );
  }

  Future<void> _ignore(DeviceUser u) async {
    final ok = await confirmAction(
      context,
      title: 'Ignore this user?',
      message: 'Punches of ${u.name ?? 'number ${u.deviceUserId}'} will not be used for attendance '
          'and the user will not be matched automatically.',
      confirmLabel: 'Ignore',
    );
    if (!ok || !mounted) return;
    await _apply(
      () => ref.read(deviceRepositoryProvider).mapUser(u.id, userId: null, allDevices: false),
      'Ignored.',
    );
  }

  Future<void> _reset(DeviceUser u) => _apply(
        () => ref.read(deviceRepositoryProvider).resetToAuto(u.id),
        'Back to automatic matching.',
      );

  @override
  Widget build(BuildContext context) {
    final canMap = canMapDevices(ref.watch(adminPermissionsProvider));
    final async = ref.watch(deviceUsersProvider(_key));
    final notifier = ref.read(deviceUsersProvider(_key).notifier);
    final devices = ref.watch(devicesProvider).valueOrNull ?? const <Device>[];
    return AdminPagedBody<DeviceUser>(
      async: async,
      onRefresh: notifier.reload,
      onLoadMore: notifier.loadMore,
      emptyMessage: _status == 'unmatched' ? 'Every device user is linked to an employee.' : 'No device users match.',
      emptyIcon: Icons.people_outline,
      header: Padding(
        padding: const EdgeInsets.fromLTRB(12, 12, 12, 0),
        child: Wrap(
          spacing: 8,
          runSpacing: 8,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            for (final e in deviceUserStatuses.entries)
              ChoiceChip(
                key: ValueKey('users-${e.key}'),
                label: Text(e.value),
                selected: _status == e.key,
                onSelected: (_) => setState(() => _status = e.key),
              ),
            if (devices.length > 1)
              DropdownButton<String?>(
                value: _deviceId,
                hint: const Text('All devices'),
                items: [
                  const DropdownMenuItem<String?>(child: Text('All devices')),
                  for (final d in devices) DropdownMenuItem<String?>(value: d.id, child: Text(d.name)),
                ],
                onChanged: (v) => setState(() => _deviceId = v),
              ),
            SizedBox(
              width: 220,
              child: TextField(
                decoration: const InputDecoration(labelText: 'Search number or name', prefixIcon: Icon(Icons.search)),
                onSubmitted: (v) => setState(() => _q = v.trim()),
              ),
            ),
          ],
        ),
      ),
      itemBuilder: (context, u) => DeviceUserCard(
        user: u,
        busy: _busy,
        onMap: canMap ? () => _map(u) : null,
        onIgnore: canMap && !u.isIgnored ? () => _ignore(u) : null,
        onReset: canMap && (u.isIgnored || (u.isMatched && u.matchMethod == 'manual')) ? () => _reset(u) : null,
      ),
    );
  }
}

class DeviceUserCard extends StatelessWidget {
  const DeviceUserCard({required this.user, super.key, this.onMap, this.onIgnore, this.onReset, this.busy = false});
  final DeviceUser user;
  final VoidCallback? onMap;
  final VoidCallback? onIgnore;
  final VoidCallback? onReset;
  final bool busy;

  @override
  Widget build(BuildContext context) {
    final u = user;
    final theme = Theme.of(context);
    final linked = u.user == null ? '' : '  ->  ${u.user!.display}';
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('${u.name ?? 'No name'}  (number ${u.deviceUserId})$linked', style: theme.textTheme.titleSmall),
            Text(
              [
                u.device?.name ?? '',
                deviceUserStatuses[u.status] ?? u.status,
                if (u.cardMasked != null) 'card ${u.cardMasked}',
                if (u.isDeviceAdmin) 'can change machine settings',
                if (!u.presentOnDevice) 'no longer on the machine',
              ].where((e) => e.isNotEmpty).join('  -  '),
              style: theme.textTheme.bodySmall,
            ),
            Text(
              'Last punch ${u.lastPunchAt == null ? 'never' : '${formatDayLong(u.lastPunchAt!)} ${formatTime(u.lastPunchAt!)}'}'
              '${u.unappliedPunches > 0 ? '  -  ${u.unappliedPunches} not yet used' : ''}',
              style: theme.textTheme.bodySmall,
            ),
            if (onMap != null || onIgnore != null || onReset != null)
              Wrap(
                spacing: 8,
                children: [
                  if (onMap != null)
                    TextButton(
                      key: ValueKey('map-${u.id}'),
                      onPressed: busy ? null : onMap,
                      child: Text(u.isMatched ? 'Change' : 'Link to employee'),
                    ),
                  if (onIgnore != null)
                    TextButton(key: ValueKey('ignore-${u.id}'), onPressed: busy ? null : onIgnore, child: const Text('Ignore')),
                  if (onReset != null)
                    TextButton(key: ValueKey('reset-${u.id}'), onPressed: busy ? null : onReset, child: const Text('Reset to automatic')),
                ],
              ),
          ],
        ),
      ),
    );
  }
}
