import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/utils/dates.dart';
import '../../admin/domain/admin_models.dart' show Employee;
import '../../admin/presentation/admin_widgets.dart' show AdminPagedBody, showAdminSnack;
import '../../admin/presentation/employee_picker.dart';
import '../domain/device_models.dart';
import 'device_providers.dart';

/// Read-only punch log (`device.view`): filters for device, employee, device
/// user number, date range (at most 92 days) and outcome.
class PunchLogTab extends ConsumerStatefulWidget {
  const PunchLogTab({super.key, this.now});
  final DateTime Function()? now;

  @override
  ConsumerState<PunchLogTab> createState() => _PunchLogTabState();
}

class _PunchLogTabState extends ConsumerState<PunchLogTab> {
  late DateTime _to = _today;
  late DateTime _from = _today.subtract(const Duration(days: 6));
  String? _deviceId;
  Employee? _employee;
  String? _outcome;
  String _number = '';

  DateTime get _today {
    final n = (widget.now ?? DateTime.now)();
    return DateTime(n.year, n.month, n.day);
  }

  PunchQuery get _key => PunchQuery(
        from: ymd(_from),
        to: ymd(_to),
        deviceId: _deviceId,
        employeeId: _employee?.id,
        deviceUserId: _number,
        outcome: _outcome,
      );

  Future<void> _pickRange() async {
    final r = await showDateRangePicker(
      context: context,
      firstDate: _today.subtract(const Duration(days: 1100)),
      lastDate: _today,
      initialDateRange: DateTimeRange(start: _from, end: _to),
    );
    if (r == null || !mounted) return;
    final err = validatePunchRange(r.start, r.end);
    if (err != null) {
      showAdminSnack(context, err);
      return;
    }
    setState(() {
      _from = r.start;
      _to = r.end;
    });
  }

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(devicePunchesProvider(_key));
    final notifier = ref.read(devicePunchesProvider(_key).notifier);
    final devices = ref.watch(devicesProvider).valueOrNull ?? const <Device>[];
    return AdminPagedBody<DevicePunch>(
      async: async,
      onRefresh: notifier.reload,
      onLoadMore: notifier.loadMore,
      emptyMessage: 'No punches in this period.',
      emptyIcon: Icons.touch_app_outlined,
      header: Padding(
        padding: const EdgeInsets.fromLTRB(12, 12, 12, 0),
        child: Wrap(
          spacing: 8,
          runSpacing: 8,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            OutlinedButton.icon(
              key: const ValueKey('punch-range'),
              onPressed: _pickRange,
              icon: const Icon(Icons.date_range),
              label: Text('${ymd(_from)} to ${ymd(_to)}'),
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
            DropdownButton<String?>(
              key: const ValueKey('punch-outcome'),
              value: _outcome,
              hint: const Text('Any result'),
              items: [
                const DropdownMenuItem<String?>(child: Text('Any result')),
                for (final o in punchOutcomeFilters) DropdownMenuItem<String?>(value: o, child: Text(punchOutcomeLabel(o))),
              ],
              onChanged: (v) => setState(() => _outcome = v),
            ),
            InputChip(
              label: Text(_employee == null ? 'Any employee' : _employee!.fullName),
              avatar: const Icon(Icons.person_search_outlined, size: 18),
              onPressed: () async {
                final e = await pickEmployee(context, title: 'Employee');
                if (e != null) setState(() => _employee = e);
              },
              onDeleted: _employee == null ? null : () => setState(() => _employee = null),
            ),
            SizedBox(
              width: 160,
              child: TextField(
                decoration: const InputDecoration(labelText: 'Device user number'),
                keyboardType: TextInputType.number,
                onSubmitted: (v) => setState(() => _number = v.trim()),
              ),
            ),
          ],
        ),
      ),
      itemBuilder: (context, p) => PunchTile(punch: p),
    );
  }
}

class PunchTile extends StatelessWidget {
  const PunchTile({required this.punch, super.key});
  final DevicePunch punch;

  @override
  Widget build(BuildContext context) {
    final p = punch;
    final theme = Theme.of(context);
    final bad = p.outcome == 'quarantined' || p.outcome == 'unmatched' || p.outcome == 'not_applied';
    final who = p.user?.display ?? 'Device user ${p.deviceUserId}';
    final when = p.punchAt == null ? '-' : '${ymd(p.punchAt!)} ${formatTime(p.punchAt!)}';
    final reason = punchReasonLabel(p.reason);
    return Card(
      child: ListTile(
        dense: true,
        title: Text('$when  -  $who'),
        subtitle: Text(
          '${p.device?.name ?? ''}  -  ${punchMethodLabel(p.method)}'
          '${reason.isEmpty ? '' : '\nReason: $reason'}',
        ),
        isThreeLine: reason.isNotEmpty,
        trailing: Text(
          punchOutcomeLabel(p.outcome),
          key: ValueKey('outcome-${p.id}'),
          style: TextStyle(color: bad ? theme.colorScheme.error : null),
        ),
      ),
    );
  }
}
