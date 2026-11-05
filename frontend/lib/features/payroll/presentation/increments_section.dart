import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/utils/dates.dart';
import '../../admin/presentation/admin_widgets.dart' show AdminPagedBody;
import '../domain/phase4a_models.dart';
import 'batch_screens.dart';
import 'bulk_hike_wizard_screen.dart';
import 'increment_detail_screen.dart';
import 'increment_widgets.dart';
import 'payroll_providers.dart';

/// "Increments" segment of Pay items (contract 18.4 / 18.5): every salary
/// increment with its state, plus the bulk hike and the batches. A single
/// increment is created from the employee's payroll screen.
class IncrementsSection extends ConsumerStatefulWidget {
  const IncrementsSection({required this.canManage, super.key});
  final bool canManage;

  @override
  ConsumerState<IncrementsSection> createState() => _IncrementsSectionState();
}

class _IncrementsSectionState extends ConsumerState<IncrementsSection> {
  IncrementQuery _query = const IncrementQuery();

  @override
  Widget build(BuildContext context) {
    final q = _query;
    final async = ref.watch(incrementsProvider(q));
    final thisMonth = monthKey(ref.read(payrollClockProvider)());
    final months = [for (var i = -12; i <= 12; i++) shiftMonth(thisMonth, i)];
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 12, 0),
          child: Wrap(
            spacing: 8,
            runSpacing: 8,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              SizedBox(
                width: 170,
                child: DropdownButtonFormField<String?>(
                  key: const ValueKey('inc-status'),
                  initialValue: q.status,
                  isExpanded: true,
                  decoration: const InputDecoration(labelText: 'Status', isDense: true, border: OutlineInputBorder()),
                  items: [
                    const DropdownMenuItem<String?>(child: Text('All')),
                    for (final s in const ['draft', 'submitted', 'approved', 'rejected', 'cancelled'])
                      DropdownMenuItem<String?>(value: s, child: Text(incrementStatusLabel(s))),
                  ],
                  onChanged: (v) => setState(() => _query = q.copyWith(status: () => v)),
                ),
              ),
              SizedBox(
                width: 170,
                child: DropdownButtonFormField<String?>(
                  key: const ValueKey('inc-month'),
                  initialValue: q.effectiveFrom,
                  isExpanded: true,
                  decoration: const InputDecoration(labelText: 'Month', isDense: true, border: OutlineInputBorder()),
                  items: [
                    const DropdownMenuItem<String?>(child: Text('All months')),
                    for (final m in months) DropdownMenuItem<String?>(value: m, child: Text(monthTitle(m))),
                  ],
                  onChanged: (v) => setState(() => _query = q.copyWith(effectiveFrom: () => v)),
                ),
              ),
              if (widget.canManage)
                FilledButton.tonalIcon(
                  key: const ValueKey('bulk-hike'),
                  onPressed: () async {
                    await Navigator.of(context).push<void>(
                      MaterialPageRoute<void>(builder: (_) => const BulkHikeWizardScreen()),
                    );
                    ref.invalidate(incrementsProvider);
                  },
                  icon: const Icon(Icons.groups_outlined),
                  label: const Text('Bulk hike'),
                ),
              OutlinedButton.icon(
                key: const ValueKey('batches'),
                onPressed: () => Navigator.of(context).push<void>(
                  MaterialPageRoute<void>(builder: (_) => const IncrementBatchesScreen()),
                ),
                icon: const Icon(Icons.layers_outlined),
                label: const Text('Batches'),
              ),
            ],
          ),
        ),
        Expanded(
          child: AdminPagedBody<Increment>(
            async: async,
            emptyMessage: 'No increments here. Create one from an employee in the Employees tab, '
                'or start a bulk hike.',
            emptyIcon: Icons.trending_up,
            onRefresh: () async => ref.read(incrementsProvider(q).notifier).reload(),
            onLoadMore: () => ref.read(incrementsProvider(q).notifier).loadMore(),
            itemBuilder: (context, i) => IncrementTile(
              increment: i,
              onTap: () async {
                await Navigator.of(context).push<void>(
                  MaterialPageRoute<void>(builder: (_) => IncrementDetailScreen(incrementId: i.id)),
                );
                ref.invalidate(incrementsProvider(q));
              },
            ),
          ),
        ),
      ],
    );
  }
}
