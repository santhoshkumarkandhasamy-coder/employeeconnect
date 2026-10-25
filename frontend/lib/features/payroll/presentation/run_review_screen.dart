import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart'
    show adminPermissionsProvider, confirmAction, runWithReauth;
import '../data/payroll_repository.dart';
import '../domain/payroll_models.dart';
import '../domain/payroll_money.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';
import 'payslip_edit_screen.dart';

/// Review of one run: totals, per-employee rows with errors / warnings, and the
/// lifecycle actions allowed for the current status and permissions.
class RunReviewScreen extends ConsumerStatefulWidget {
  const RunReviewScreen({required this.runId, super.key});
  final String runId;

  @override
  ConsumerState<RunReviewScreen> createState() => _RunReviewScreenState();
}

class _RunReviewScreenState extends ConsumerState<RunReviewScreen> {
  final _scroll = ScrollController();
  final _search = TextEditingController();
  Timer? _debounce;
  String _q = '';
  String? _flag;
  bool _busy = false;

  RunPayslipQuery get _query => RunPayslipQuery(widget.runId, q: _q, flag: _flag);

  @override
  void initState() {
    super.initState();
    _scroll.addListener(() {
      if (_scroll.hasClients && _scroll.position.extentAfter < 300) {
        unawaited(ref.read(runPayslipsProvider(_query).notifier).loadMore());
      }
    });
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _scroll.dispose();
    _search.dispose();
    super.dispose();
  }

  void _reload() {
    ref.invalidate(payrollRunProvider(widget.runId));
    ref.invalidate(runPayslipsProvider);
  }

  /// Runs a lifecycle call, then reloads. Blocked processing lists the
  /// employees; any other error is shown as a message.
  Future<void> _act(Future<void> Function(PayrollRepository repo) call, {String? done}) async {
    if (_busy) return;
    setState(() => _busy = true);
    final repo = ref.read(payrollRepositoryProvider);
    try {
      await call(repo);
      _reload();
      if (mounted && done != null) payrollSnack(context, done);
    } on _Cancelled {
      // the user closed the password prompt
    } on ApiException catch (e) {
      if (!mounted) return;
      if (e.code == 'PAYROLL_BLOCKED') {
        unawaited(showBlockedDialog(context, e));
      } else {
        payrollSnack(context, payrollErrorMessage(e));
        // The run moved on (published / already processed): show its real state.
        if (e.code == 'PAYROLL_LOCKED' || e.code == 'CONFLICT') _reload();
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _publish(PayrollRun run) async {
    final ok = await confirmAction(
      context,
      title: 'Publish ${periodTitle(run.period)}?',
      message: 'Every processed payslip is published to its employee, who is notified. '
          'The run is locked afterwards; later corrections are made as revisions.',
      confirmLabel: 'Publish',
    );
    if (!ok || !mounted) return;
    await _act((repo) async {
      final done = await runWithReauth(
        context,
        reason: 'Publishing payslips needs your password.',
        action: (pw) async {
          await repo.publishRun(run.id, currentPassword: pw);
        },
      );
      if (!done) throw const _Cancelled();
    }, done: 'Payslips published.',);
  }

  Future<void> _delete(PayrollRun run) async {
    final ok = await confirmAction(
      context,
      title: 'Delete this draft run?',
      message: 'All draft payslips of ${periodTitle(run.period)} are removed. '
          'You can create the run again.',
      confirmLabel: 'Delete',
    );
    if (!ok || !mounted) return;
    final repo = ref.read(payrollRepositoryProvider);
    final nav = Navigator.of(context);
    try {
      await repo.deleteRun(run.id);
      ref.invalidate(payrollRunsProvider);
      nav.pop();
    } on ApiException catch (e) {
      if (mounted) payrollSnack(context, payrollErrorMessage(e));
    }
  }

  Future<void> _register(PayrollRun run) async {
    final perms = ref.read(adminPermissionsProvider);
    final includeBank = await showDialog<bool>(
      context: context,
      builder: (_) => _RegisterDialog(canIncludeBank: canManagePayroll(perms)),
    );
    if (includeBank == null || !mounted) return;
    await downloadPayrollFile(context, ref, (repo) async {
      if (!includeBank) return repo.register(run.id, run.period);
      PayrollFile? file;
      final done = await runWithReauth(
        context,
        reason: 'Bank details in the register need your password.',
        action: (pw) async {
          file = await repo.register(run.id, run.period, includeBank: true, currentPassword: pw);
        },
      );
      return done ? file : null;
    });
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!canOpenPayroll(perms)) {
      return const Scaffold(body: EmptyView(message: 'You do not have permission to view this.'));
    }
    final runAsync = ref.watch(payrollRunProvider(widget.runId));
    final rowsAsync = ref.watch(runPayslipsProvider(_query));

    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(
          title: Text(runAsync.valueOrNull == null
              ? 'Payroll run'
              : periodTitle(runAsync.valueOrNull!.period),),
        ),
        body: runAsync.when(
          skipLoadingOnReload: true,
          loading: () => const LoadingView(label: 'Loading run'),
          error: (e, _) => ErrorView(message: payrollMsg(e), onRetry: _reload),
          data: (run) => RefreshIndicator(
            onRefresh: () async {
              _reload();
              await ref.read(payrollRunProvider(widget.runId).future).then((_) {}, onError: (Object _) {});
            },
            child: ListView(
              controller: _scroll,
              physics: const AlwaysScrollableScrollPhysics(),
              padding: const EdgeInsets.fromLTRB(12, 8, 12, 48),
              children: [
                _RunSummary(run: run),
                _Actions(
                  run: run,
                  perms: perms,
                  busy: _busy,
                  onRefresh: () => _act((r) => r.refreshRun(run.id), done: 'Run refreshed.'),
                  onProcess: () => _act((r) => r.processRun(run.id), done: 'Run processed.'),
                  onReopen: () => _act((r) => r.reopenRun(run.id), done: 'Run reopened as draft.'),
                  onPublish: () => _publish(run),
                  onDelete: () => _delete(run),
                  onRegister: () => _register(run),
                ),
                const SizedBox(height: 8),
                TextField(
                  controller: _search,
                  textInputAction: TextInputAction.search,
                  decoration: const InputDecoration(
                    labelText: 'Search name or ID',
                    prefixIcon: Icon(Icons.search),
                    border: OutlineInputBorder(),
                    isDense: true,
                  ),
                  onChanged: (v) {
                    _debounce?.cancel();
                    _debounce = Timer(const Duration(milliseconds: 400), () {
                      if (mounted) setState(() => _q = v);
                    });
                  },
                ),
                const SizedBox(height: 8),
                SingleChildScrollView(
                  scrollDirection: Axis.horizontal,
                  child: Row(
                    children: [
                      for (final f in const <(String?, String)>[
                        (null, 'All'),
                        ('error', 'Errors'),
                        ('warning', 'Warnings'),
                        ('hold', 'On hold'),
                      ])
                        Padding(
                          padding: const EdgeInsets.only(right: 8),
                          child: ChoiceChip(
                            label: Text(f.$2),
                            selected: _flag == f.$1,
                            onSelected: (_) => setState(() => _flag = f.$1),
                          ),
                        ),
                    ],
                  ),
                ),
                const SizedBox(height: 4),
                ...rowsAsync.when(
                  skipLoadingOnReload: true,
                  loading: () => const <Widget>[
                    Padding(padding: EdgeInsets.all(32), child: LoadingView(label: 'Loading payslips')),
                  ],
                  error: (e, _) => <Widget>[
                    ErrorView(
                      message: payrollMsg(e),
                      onRetry: () => ref.invalidate(runPayslipsProvider(_query)),
                    ),
                  ],
                  data: (s) => <Widget>[
                    if (s.items.isEmpty)
                      const EmptyView(
                        icon: Icons.receipt_long_outlined,
                        message: 'No payslips match.',
                      ),
                    for (final r in s.items)
                      _PayslipRowTile(
                        row: r,
                        onChanged: _reload,
                      ),
                    if (s.loadingMore)
                      const Padding(
                        padding: EdgeInsets.all(16),
                        child: Center(child: CircularProgressIndicator()),
                      ),
                    if (s.loadMoreError != null)
                      TextButton(
                        onPressed: () => ref.read(runPayslipsProvider(_query).notifier).loadMore(),
                        child: Text('${s.loadMoreError} Try again'),
                      ),
                  ],
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _Cancelled implements Exception {
  const _Cancelled();
}

class _RunSummary extends StatelessWidget {
  const _RunSummary({required this.run});
  final PayrollRun run;

  @override
  Widget build(BuildContext context) {
    final c = run.counts;
    final t = run.totals;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                StatusChip(run.status),
                const SizedBox(width: 8),
                Expanded(child: Text('${c.employees} employees')),
              ],
            ),
            const SizedBox(height: 8),
            AmountRow(label: 'Gross earnings', value: formatRupees(t.grossEarnings)),
            AmountRow(label: 'Deductions', value: formatRupees(t.totalDeductions)),
            AmountRow(label: 'Net pay', value: formatRupees(t.netPay), bold: true),
            const Divider(),
            Wrap(
              spacing: 12,
              runSpacing: 4,
              children: [
                Text('Draft ${c.draft}'),
                Text('Processed ${c.processed}'),
                Text('Published ${c.published}'),
                if (c.held > 0) Text('On hold ${c.held}'),
                if (c.errors > 0)
                  Text(
                    'Errors ${c.errors}',
                    style: TextStyle(color: Theme.of(context).colorScheme.error),
                  ),
                if (c.warnings > 0) Text('Warnings ${c.warnings}'),
                if (c.openRevisions > 0) Text('Open revisions ${c.openRevisions}'),
              ],
            ),
            if (run.attendanceCutoff != null)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(
                  'Absent days are counted up to ${run.attendanceCutoff}.',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _Actions extends StatelessWidget {
  const _Actions({
    required this.run,
    required this.perms,
    required this.busy,
    required this.onRefresh,
    required this.onProcess,
    required this.onReopen,
    required this.onPublish,
    required this.onDelete,
    required this.onRegister,
  });
  final PayrollRun run;
  final List<String> perms;
  final bool busy;
  final VoidCallback onRefresh;
  final VoidCallback onProcess;
  final VoidCallback onReopen;
  final VoidCallback onPublish;
  final VoidCallback onDelete;
  final VoidCallback onRegister;

  @override
  Widget build(BuildContext context) {
    final manage = canManagePayroll(perms);
    final publish = canPublishPayroll(perms);
    final buttons = <Widget>[
      if (manage && run.isDraft)
        OutlinedButton.icon(
          onPressed: busy ? null : onRefresh,
          icon: const Icon(Icons.refresh),
          label: const Text('Refresh'),
        ),
      if (manage && run.isDraft)
        FilledButton.icon(
          key: const ValueKey('process-run'),
          onPressed: busy ? null : onProcess,
          icon: const Icon(Icons.calculate_outlined),
          label: const Text('Process'),
        ),
      if (manage && run.isProcessed)
        OutlinedButton.icon(
          onPressed: busy ? null : onReopen,
          icon: const Icon(Icons.undo),
          label: const Text('Reopen'),
        ),
      if (publish && run.isProcessed)
        FilledButton.icon(
          key: const ValueKey('publish-run'),
          onPressed: busy ? null : onPublish,
          icon: const Icon(Icons.send_outlined),
          label: const Text('Publish'),
        ),
      OutlinedButton.icon(
        key: const ValueKey('register-run'),
        onPressed: busy ? null : onRegister,
        icon: const Icon(Icons.table_chart_outlined),
        label: const Text('Register'),
      ),
      if (manage && run.isDraft && run.counts.published == 0)
        TextButton.icon(
          onPressed: busy ? null : onDelete,
          icon: const Icon(Icons.delete_outline),
          label: const Text('Delete run'),
        ),
    ];
    return Padding(
      padding: const EdgeInsets.only(top: 4),
      child: Wrap(spacing: 8, runSpacing: 8, children: buttons),
    );
  }
}

class _RegisterDialog extends StatefulWidget {
  const _RegisterDialog({required this.canIncludeBank});
  final bool canIncludeBank;

  @override
  State<_RegisterDialog> createState() => _RegisterDialogState();
}

class _RegisterDialogState extends State<_RegisterDialog> {
  bool _bank = false;

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: const Text('Payroll register'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('An Excel workbook with every employee, the totals and a summary. '
                'It contains salary data, so share it only with people who need it.',),
            if (widget.canIncludeBank)
              CheckboxListTile(
                key: const ValueKey('include-bank'),
                contentPadding: EdgeInsets.zero,
                value: _bank,
                onChanged: (v) => setState(() => _bank = v ?? false),
                title: const Text('Include bank details'),
                subtitle: const Text('Asks for your password.'),
              ),
          ],
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            onPressed: () => Navigator.pop(context, _bank),
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: const Text('Download'),
          ),
        ],
      );
}

class _PayslipRowTile extends ConsumerWidget {
  const _PayslipRowTile({required this.row, required this.onChanged});
  final RunPayslipRow row;
  final VoidCallback onChanged;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final u = row.user;
    final theme = Theme.of(context);
    return Card(
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: InkWell(
        onTap: () async {
          await Navigator.of(context).push<void>(
            MaterialPageRoute<void>(builder: (_) => PayslipEditScreen(payslipId: row.id)),
          );
          onChanged();
        },
        child: Padding(
          padding: const EdgeInsets.fromLTRB(14, 10, 4, 10),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      u == null ? '-' : (u.fullName.isEmpty ? u.employeeId : u.fullName),
                      style: theme.textTheme.titleSmall,
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                  StatusChip(row.status),
                  PopupMenuButton<String>(
                    tooltip: 'More',
                    onSelected: (v) {
                      if (v == 'pdf') {
                        unawaited(downloadPayrollFile(context, ref, (repo) => repo.payslipPdf(row.id)));
                      }
                    },
                    itemBuilder: (_) => const [
                      PopupMenuItem(value: 'pdf', child: Text('Preview PDF')),
                    ],
                  ),
                ],
              ),
              Text(
                [
                  if (u != null) u.employeeId,
                  'Paid ${formatDays(row.paidDays)} d',
                  'LOP ${formatDays(row.lopDays)} d',
                  if (row.revision > 1) 'Rev ${row.revision}',
                ].join('  -  '),
                style: theme.textTheme.bodySmall,
              ),
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 4),
                child: Text('Net ${netPayLabel(row.netPay)}', style: theme.textTheme.titleMedium),
              ),
              if (row.errors.isNotEmpty || row.warnings.isNotEmpty || row.hold)
                FlagChips(errors: row.errors, warnings: row.warnings, hold: row.hold),
            ],
          ),
        ),
      ),
    );
  }
}

String netPayLabel(String? net) => net == null ? '-' : formatRupees(net);
