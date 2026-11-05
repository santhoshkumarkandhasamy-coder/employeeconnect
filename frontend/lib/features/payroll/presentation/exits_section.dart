import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/utils/dates.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import '../../admin/presentation/admin_widgets.dart' show AdminPagedBody, adminSelfIdProvider;
import '../domain/phase4b_models.dart';
import 'fnf_detail_screen.dart';
import 'fnf_widgets.dart';
import 'pay_items_widgets.dart';
import 'payroll_providers.dart';
import 'record_exit_screen.dart';

/// "Exits" segment of Pay items (contract 18.8): every full and final
/// settlement with its state, an alert when someone left without one, and
/// "Record exit" for people with `payroll.manage`.
class ExitsSection extends ConsumerStatefulWidget {
  const ExitsSection({required this.canManage, super.key});
  final bool canManage;

  @override
  ConsumerState<ExitsSection> createState() => _ExitsSectionState();
}

class _ExitsSectionState extends ConsumerState<ExitsSection> {
  FnfQuery _query = const FnfQuery();

  Future<void> _recordExit() async {
    final picked = await showDialog<EmployeeRef>(context: context, builder: (_) => const EmployeePickerDialog());
    if (picked == null || !mounted) return;
    if (picked.id == ref.read(adminSelfIdProvider)) return;
    await Navigator.of(context).push<void>(
      MaterialPageRoute<void>(builder: (_) => RecordExitScreen(employee: picked)),
    );
    ref.invalidate(fnfListProvider);
  }

  @override
  Widget build(BuildContext context) {
    final q = _query;
    final async = ref.watch(fnfListProvider(q));
    final period = monthKey(ref.read(payrollClockProvider)());
    final dash = ref.watch(payrollDashboardProvider(period)).valueOrNull;
    final calendar = dash?['calendar'];
    final List<Object?> cal = calendar is Map && calendar['alerts'] is List
        ? (calendar['alerts'] as List).cast<Object?>()
        : const [];
    final withoutFnf = cal.contains('EXIT_WITHOUT_FNF');
    final pending = cal.contains('FNF_PENDING_APPROVAL');
    return Column(
      children: [
        if (withoutFnf || pending)
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 0),
            child: Card(
              key: const ValueKey('exit-alert'),
              color: Theme.of(context).colorScheme.errorContainer,
              child: ListTile(
                leading: const Icon(Icons.warning_amber_outlined),
                title: Text(
                  withoutFnf
                      ? 'Someone has left without a final settlement, or with an active loan. Record the exit.'
                      : 'A final settlement is waiting for approval.',
                ),
              ),
            ),
          ),
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 12, 0),
          child: Wrap(
            spacing: 8,
            runSpacing: 8,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              SizedBox(
                width: 190,
                child: DropdownButtonFormField<String?>(
                  key: const ValueKey('fnf-status'),
                  initialValue: q.status,
                  isExpanded: true,
                  decoration: const InputDecoration(labelText: 'Status', isDense: true, border: OutlineInputBorder()),
                  items: [
                    const DropdownMenuItem<String?>(child: Text('All')),
                    for (final s in fnfStatusLabels.keys)
                      DropdownMenuItem<String?>(value: s, child: Text(fnfStatusLabel(s))),
                  ],
                  onChanged: (v) => setState(() => _query = q.copyWith(status: () => v)),
                ),
              ),
              if (widget.canManage)
                FilledButton.tonalIcon(
                  key: const ValueKey('record-exit'),
                  onPressed: _recordExit,
                  icon: const Icon(Icons.logout),
                  label: const Text('Record exit'),
                ),
            ],
          ),
        ),
        Expanded(
          child: AdminPagedBody<FnfListItem>(
            async: async,
            emptyMessage: 'No final settlements yet. Use Record exit when an employee leaves.',
            emptyIcon: Icons.logout,
            onRefresh: () async => ref.read(fnfListProvider(q).notifier).reload(),
            onLoadMore: () => ref.read(fnfListProvider(q).notifier).loadMore(),
            itemBuilder: (context, i) => FnfTile(
              item: i,
              onTap: () async {
                await Navigator.of(context).push<void>(
                  MaterialPageRoute<void>(builder: (_) => FnfDetailScreen(fnfId: i.id)),
                );
                ref.invalidate(fnfListProvider(q));
              },
            ),
          ),
        ),
      ],
    );
  }
}
