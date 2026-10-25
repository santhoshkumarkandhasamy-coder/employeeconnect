import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/month_selector.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart' show adminPermissionsProvider;
import '../data/payroll_repository.dart';
import '../domain/payroll_models.dart';
import '../domain/payroll_money.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';
import 'run_review_screen.dart';

/// Payroll runs of one year, newest first, plus "New run" (`payroll.manage`).
class RunsTab extends ConsumerStatefulWidget {
  const RunsTab({super.key});

  @override
  ConsumerState<RunsTab> createState() => _RunsTabState();
}

class _RunsTabState extends ConsumerState<RunsTab> {
  late int _year = ref.read(payrollClockProvider)().year;
  bool _creating = false;

  Future<void> _newRun() async {
    final now = ref.read(payrollClockProvider)();
    final picked = await showDialog<({String period, String? payDate})>(
      context: context,
      builder: (_) => NewRunDialog(now: now),
    );
    if (picked == null || !mounted || _creating) return;
    setState(() => _creating = true);
    final repo = ref.read(payrollRepositoryProvider);
    try {
      final run = await repo.createRun(picked.period, payDate: picked.payDate);
      ref.invalidate(payrollRunsProvider);
      if (!mounted) return;
      await Navigator.of(context).push<void>(
        MaterialPageRoute<void>(builder: (_) => RunReviewScreen(runId: run.id)),
      );
      ref.invalidate(payrollRunsProvider);
    } on ApiException catch (e) {
      if (mounted) payrollSnack(context, payrollErrorMessage(e));
    } finally {
      if (mounted) setState(() => _creating = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    final now = ref.read(payrollClockProvider)();
    final async = ref.watch(payrollRunsProvider(_year));
    return Scaffold(
      backgroundColor: Colors.transparent,
      floatingActionButton: canManagePayroll(perms)
          ? FloatingActionButton.extended(
              onPressed: _creating ? null : _newRun,
              icon: const Icon(Icons.add),
              label: const Text('New run'),
            )
          : null,
      body: Column(
        children: [
          YearSelector(year: _year, maxYear: now.year, onChanged: (y) => setState(() => _year = y)),
          Expanded(
            child: async.when(
              skipLoadingOnReload: true,
              loading: () => const LoadingView(label: 'Loading runs'),
              error: (e, _) => ErrorView(
                message: payrollMsg(e),
                onRetry: () => ref.invalidate(payrollRunsProvider(_year)),
              ),
              data: (runs) => RefreshIndicator(
                onRefresh: () async {
                  ref.invalidate(payrollRunsProvider(_year));
                  await ref.read(payrollRunsProvider(_year).future).then((_) {}, onError: (Object _) {});
                },
                child: runs.isEmpty
                    ? ListView(
                        physics: const AlwaysScrollableScrollPhysics(),
                        children: const [
                          SizedBox(height: 80),
                          EmptyView(
                            icon: Icons.payments_outlined,
                            message: 'No payroll runs for this year.',
                          ),
                        ],
                      )
                    : ListView.builder(
                        physics: const AlwaysScrollableScrollPhysics(),
                        padding: const EdgeInsets.fromLTRB(12, 4, 12, 96),
                        itemCount: runs.length,
                        itemBuilder: (_, i) => _RunTile(run: runs[i], year: _year),
                      ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _RunTile extends ConsumerWidget {
  const _RunTile({required this.run, required this.year});
  final PayrollRunItem run;
  final int year;

  @override
  Widget build(BuildContext context, WidgetRef ref) => Card(
        margin: const EdgeInsets.symmetric(vertical: 4),
        child: ListTile(
          title: Text(periodTitle(run.period)),
          subtitle: Text('${run.employees} employees  -  Net ${formatRupees(run.netPayTotal)}'),
          trailing: Row(
            mainAxisSize: MainAxisSize.min,
            children: [StatusChip(run.status), const Icon(Icons.chevron_right)],
          ),
          onTap: () async {
            await Navigator.of(context).push<void>(
              MaterialPageRoute<void>(builder: (_) => RunReviewScreen(runId: run.id)),
            );
            ref.invalidate(payrollRunsProvider(year));
          },
        ),
      );
}

/// Month picker (past or current month) and an optional pay date.
class NewRunDialog extends StatefulWidget {
  const NewRunDialog({required this.now, super.key});
  final DateTime now;

  @override
  State<NewRunDialog> createState() => _NewRunDialogState();
}

class _NewRunDialogState extends State<NewRunDialog> {
  late String _month = monthKey(widget.now);
  DateTime? _payDate;

  Future<void> _pickDate() async {
    final base = parseMonth(_month) ?? widget.now;
    final d = await showDatePicker(
      context: context,
      initialDate: _payDate ?? DateTime(base.year, base.month + 1, 0),
      firstDate: DateTime(base.year - 1),
      lastDate: DateTime(base.year + 2, 12, 31),
    );
    if (d != null) setState(() => _payDate = d);
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: const Text('New payroll run'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            MonthSelector(
              month: _month,
              now: widget.now,
              onChanged: (m) => setState(() => _month = m),
            ),
            const SizedBox(height: 8),
            ListTile(
              contentPadding: EdgeInsets.zero,
              leading: const Icon(Icons.event_outlined),
              title: Text(_payDate == null ? 'Pay date (optional)' : formatDayLong(_payDate!)),
              trailing: _payDate == null
                  ? null
                  : IconButton(
                      tooltip: 'Clear pay date',
                      icon: const Icon(Icons.clear),
                      onPressed: () => setState(() => _payDate = null),
                    ),
              onTap: _pickDate,
            ),
            const Text('A draft payslip is created for every eligible employee.'),
          ],
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            onPressed: () => Navigator.pop(
              context,
              (period: _month, payDate: _payDate == null ? null : ymd(_payDate!)),
            ),
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: const Text('Create'),
          ),
        ],
      );
}
