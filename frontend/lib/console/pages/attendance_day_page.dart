import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/utils/dates.dart';
import '../../features/admin/domain/ops_models.dart';
import '../../features/admin/presentation/admin_providers.dart';
import '../../features/admin/presentation/admin_widgets.dart';

const _statuses = <String, String>{
  'all': 'All',
  'checked_in': 'Checked in',
  'late': 'Late',
  'not_checked_in': 'Not checked in',
  'on_leave': 'On leave',
};

/// `GET /admin/attendance/day`: every active employee's status for a day.
class AttendanceDayPage extends ConsumerStatefulWidget {
  const AttendanceDayPage({super.key});

  @override
  ConsumerState<AttendanceDayPage> createState() => _DayState();
}

class _DayState extends ConsumerState<AttendanceDayPage> {
  DateTime _date = DateTime.now();
  String _status = 'all';
  String _q = '';

  DayKey get _key => (ymd(_date), _status, _q);

  Future<void> _pick() async {
    final now = DateTime.now();
    final d = await showDatePicker(
      context: context,
      initialDate: _date,
      firstDate: now.subtract(const Duration(days: 366)),
      lastDate: now,
    );
    if (d != null) setState(() => _date = d);
  }

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(attendanceDayProvider(_key));
    final notifier = ref.read(attendanceDayProvider(_key).notifier);
    return AdminPagedBody<AttendanceDayRow>(
      async: async,
      onRefresh: notifier.reload,
      onLoadMore: notifier.loadMore,
      emptyMessage: 'No employees match.',
      emptyIcon: Icons.fact_check_outlined,
      header: Padding(
        padding: const EdgeInsets.fromLTRB(12, 12, 12, 0),
        child: Wrap(
          spacing: 12,
          runSpacing: 8,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            OutlinedButton.icon(
              onPressed: _pick,
              icon: const Icon(Icons.event),
              label: Text(ymd(_date)),
            ),
            SizedBox(
              width: 220,
              child: TextField(
                decoration: const InputDecoration(
                  labelText: 'Search name or ID',
                  prefixIcon: Icon(Icons.search),
                ),
                onSubmitted: (v) => setState(() => _q = v.trim()),
              ),
            ),
            for (final e in _statuses.entries)
              ChoiceChip(
                label: Text(e.value),
                selected: _status == e.key,
                onSelected: (_) => setState(() => _status = e.key),
              ),
          ],
        ),
      ),
      itemBuilder: (context, r) => Card(
        child: ListTile(
          title: Text(r.user?.display ?? '-'),
          subtitle: Text(
            '${attendanceStatusLabel(r.status)}'
            '${r.leaveType == null ? '' : ' (${r.leaveType})'}'
            '${r.checkInAt == null ? '' : '  In ${formatTime(r.checkInAt!)}'}'
            '${r.checkOutAt == null ? '' : '  Out ${formatTime(r.checkOutAt!)}'}'
            '${r.minutesLate > 0 ? '  ${r.minutesLate} min late' : ''}',
          ),
        ),
      ),
    );
  }
}
