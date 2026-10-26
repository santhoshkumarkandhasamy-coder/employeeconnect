import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../core/security/screen_protect.dart';
import '../../../../core/utils/dates.dart';
import '../../../admin/presentation/admin_widgets.dart';
import '../../domain/duty_admin_models.dart';
import 'duty_admin_providers.dart';

/// Admin > On-duty visits: one day, filterable by site, employee, open only.
/// Distances and the suspicious flag are shown to `attendance.view_all` only.
class DutyVisitsScreen extends ConsumerStatefulWidget {
  const DutyVisitsScreen({super.key, this.embedded = false});

  /// Inside the web console shell (which already has an app bar).
  final bool embedded;

  @override
  ConsumerState<DutyVisitsScreen> createState() => _VisitsState();
}

class _VisitsState extends ConsumerState<DutyVisitsScreen> {
  DateTime _date = DateTime.now();
  String? _locationId;
  String _employee = '';
  bool _open = false;

  VisitQuery get _query =>
      VisitQuery(date: ymd(_date), locationId: _locationId, employeeId: _employee, openOnly: _open);

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
    final perms = ref.watch(adminPermissionsProvider);
    if (!canViewDutyVisits(perms)) return const NoPermissionScreen(title: 'On-duty visits');
    final detail = canSeeVisitDetail(perms);
    final sites = canManageDutyLocations(perms) ? ref.watch(dutyLocationOptionsProvider).valueOrNull : null;
    final async = ref.watch(adminVisitsProvider(_query));
    final notifier = ref.read(adminVisitsProvider(_query).notifier);

    final body = AdminPagedBody<AdminVisit>(
      async: async,
      onRefresh: notifier.reload,
      onLoadMore: notifier.loadMore,
      emptyMessage: _open ? 'No open visits.' : 'No visits on ${ymd(_date)}.',
      emptyIcon: Icons.place_outlined,
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
              width: 180,
              child: TextField(
                decoration: const InputDecoration(labelText: 'Employee ID', prefixIcon: Icon(Icons.badge_outlined)),
                onSubmitted: (v) => setState(() => _employee = v.trim()),
              ),
            ),
            if (sites != null && sites.isNotEmpty)
              SizedBox(
                width: 220,
                child: DropdownButtonFormField<String?>(
                  initialValue: sites.any((s) => s.id == _locationId) ? _locationId : null,
                  isExpanded: true,
                  decoration: const InputDecoration(labelText: 'Location'),
                  items: [
                    const DropdownMenuItem<String?>(child: Text('All locations')),
                    for (final s in sites)
                      DropdownMenuItem<String?>(value: s.id, child: Text(s.name, overflow: TextOverflow.ellipsis)),
                  ],
                  onChanged: (v) => setState(() => _locationId = v),
                ),
              ),
            FilterChip(
              label: const Text('Open now'),
              selected: _open,
              onSelected: (v) => setState(() => _open = v),
            ),
          ],
        ),
      ),
      itemBuilder: (context, v) => VisitCard(visit: v, showDetail: detail),
    );
    if (widget.embedded) return body;
    return SecureScreen(
      child: Scaffold(appBar: AppBar(title: const Text('On-duty visits')), body: body),
    );
  }
}

class VisitCard extends StatelessWidget {
  const VisitCard({required this.visit, required this.showDetail, super.key});
  final AdminVisit visit;
  final bool showDetail;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final v = visit;
    final facts = <String>[
      if (showDetail && v.checkInDistanceM != null) 'In ${v.checkInDistanceM} m from site',
      if (showDetail && v.checkOutDistanceM != null) 'Out ${v.checkOutDistanceM} m from site',
    ];
    return Card(
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(v.user?.display ?? '-', style: theme.textTheme.titleSmall),
            Text(v.visit.locationName),
            Text(adminVisitSubtitle(v), style: theme.textTheme.bodySmall),
            if (v.visit.note != null) Text(v.visit.note!, style: theme.textTheme.bodySmall),
            if (facts.isNotEmpty) Text(facts.join('  -  '), style: theme.textTheme.bodySmall),
            const SizedBox(height: 4),
            Wrap(
              spacing: 8,
              runSpacing: 4,
              children: [
                if (v.visit.open)
                  const Chip(label: Text('Open'), visualDensity: VisualDensity.compact),
                if (v.visit.autoClosed)
                  const Chip(label: Text('Auto-closed, no check-out'), visualDensity: VisualDensity.compact),
                if (showDetail && v.checkOutWithinFence == false)
                  const Chip(label: Text('Checked out away from site'), visualDensity: VisualDensity.compact),
                if (showDetail && v.suspicious)
                  Chip(
                    label: const Text('Suspicious (mock location)'),
                    visualDensity: VisualDensity.compact,
                    backgroundColor: theme.colorScheme.errorContainer,
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
