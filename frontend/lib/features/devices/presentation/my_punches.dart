import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../attendance/domain/attendance_models.dart';
import '../domain/device_models.dart';
import 'device_providers.dart';

/// Attendance -> My punches: the caller's own office-machine punches for one day.
class MyPunchesTab extends ConsumerStatefulWidget {
  const MyPunchesTab({super.key, this.now});
  final DateTime Function()? now;

  @override
  ConsumerState<MyPunchesTab> createState() => _MyPunchesTabState();
}

class _MyPunchesTabState extends ConsumerState<MyPunchesTab> {
  late DateTime _date = _today;

  DateTime get _today {
    final n = (widget.now ?? DateTime.now)();
    return DateTime(n.year, n.month, n.day);
  }

  Future<void> _pick() async {
    final d = await showDatePicker(
      context: context,
      initialDate: _date,
      firstDate: _today.subtract(const Duration(days: 366)),
      lastDate: _today,
    );
    if (d != null) setState(() => _date = d);
  }

  @override
  Widget build(BuildContext context) {
    final key = ymd(_date);
    final async = ref.watch(myPunchesProvider(key));
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 0),
          child: Row(
            children: [
              IconButton(
                tooltip: 'Previous day',
                onPressed: () => setState(() => _date = _date.subtract(const Duration(days: 1))),
                icon: const Icon(Icons.chevron_left),
              ),
              Expanded(
                child: TextButton.icon(
                  key: const ValueKey('punch-date'),
                  onPressed: _pick,
                  icon: const Icon(Icons.event),
                  label: Text(formatDayLong(_date)),
                ),
              ),
              IconButton(
                tooltip: 'Next day',
                onPressed: _date.isBefore(_today) ? () => setState(() => _date = _date.add(const Duration(days: 1))) : null,
                icon: const Icon(Icons.chevron_right),
              ),
            ],
          ),
        ),
        Expanded(
          child: async.when(
            loading: () => const LoadingView(label: 'Loading punches'),
            error: (e, _) => ErrorView(message: deviceMsg(e), onRetry: () => ref.invalidate(myPunchesProvider(key))),
            data: (list) => RefreshIndicator(
              onRefresh: () async {
                ref.invalidate(myPunchesProvider(key));
                await ref.read(myPunchesProvider(key).future).then((_) {}, onError: (Object _) {});
              },
              child: list.isEmpty
                  ? ListView(
                      physics: const AlwaysScrollableScrollPhysics(),
                      children: const [
                        SizedBox(height: 80),
                        EmptyView(icon: Icons.touch_app_outlined, message: 'No office machine punches on this day.'),
                      ],
                    )
                  : ListView(
                      physics: const AlwaysScrollableScrollPhysics(),
                      padding: const EdgeInsets.all(16),
                      children: [for (final p in list) MyPunchTile(punch: p)],
                    ),
            ),
          ),
        ),
      ],
    );
  }
}

class MyPunchTile extends StatelessWidget {
  const MyPunchTile({required this.punch, super.key});
  final MyPunch punch;

  @override
  Widget build(BuildContext context) {
    final p = punch;
    final t = p.punchAt == null ? '--:--' : formatTime(p.punchAt!);
    return Card(
      child: Semantics(
        label: '$t at ${p.deviceName}, ${punchMethodLabel(p.method)}, ${p.usedAsLabel}',
        excludeSemantics: true,
        child: ListTile(
          leading: Icon(p.usedAs == 'check_out' ? Icons.logout : (p.usedAs == 'check_in' ? Icons.login : Icons.touch_app_outlined)),
          title: Text('$t  -  ${p.deviceName}'),
          subtitle: Text('${punchMethodLabel(p.method)}  -  ${p.usedAsLabel}'),
        ),
      ),
    );
  }
}

/// Punch list of one day for the calendar / history day detail. Shows nothing
/// while loading or when there is nothing to show, so the day sheet stays calm.
class DayPunchList extends ConsumerWidget {
  const DayPunchList({required this.date, super.key});
  final String date;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(myPunchesProvider(date));
    return async.when(
      loading: () => const Padding(padding: EdgeInsets.only(top: 8), child: LinearProgressIndicator()),
      error: (_, __) => const SizedBox.shrink(),
      data: (list) => list.isEmpty
          ? const SizedBox.shrink()
          : Column(
              key: const ValueKey('day-punches'),
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const SizedBox(height: 12),
                Text('Office machine punches', style: Theme.of(context).textTheme.titleSmall),
                for (final p in list)
                  Padding(
                    padding: const EdgeInsets.only(top: 4),
                    child: Text(
                      '${p.punchAt == null ? '--:--' : formatTime(p.punchAt!)}  ${p.deviceName}  -  ${p.usedAsLabel}',
                    ),
                  ),
              ],
            ),
    );
  }
}

/// "Check-in: Device (Main entrance)  Check-out: Phone" lines for a record.
List<String> sourceLines(AttendanceRecord r) {
  String line(String label, String? source, String? device) {
    final s = sourceLabel(source);
    if (s.isEmpty) return '';
    return '$label: $s${source == 'device' && device != null ? ' ($device)' : ''}';
  }

  return [
    if (r.checkInAt != null) line('Check-in', r.checkInSource, r.checkInDeviceName),
    if (r.checkOutAt != null) line('Check-out', r.checkOutSource, r.checkOutDeviceName),
  ].where((e) => e.isNotEmpty).toList();
}

/// Day detail sheet for a history record: status, times with their source and the punch list.
void showRecordDetail(BuildContext context, AttendanceRecord r) {
  final date = parseYmd(r.workDate);
  showModalBottomSheet<void>(
    context: context,
    showDragHandle: true,
    isScrollControlled: true,
    builder: (ctx) => SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(24, 0, 24, 24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(date == null ? r.workDate : formatDayLong(date), style: Theme.of(ctx).textTheme.titleMedium),
            const SizedBox(height: 8),
            Text(r.status.label),
            for (final l in sourceLines(r)) Text(l),
            if (r.manual && r.manualReason != null && r.manualReason!.isNotEmpty) Text('HR note: ${r.manualReason}'),
            if (date != null) DayPunchList(date: r.workDate),
          ],
        ),
      ),
    ),
  );
}
