import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import '../../admin/presentation/admin_widgets.dart'
    show AdminPagedBody, adminPermissionsProvider, adminSelfIdProvider, runWithReauth;
import '../data/payroll_repository.dart';
import '../domain/pay_items_models.dart';
import '../domain/payroll_logic.dart';
import '../domain/payroll_models.dart';
import '../domain/payroll_money.dart';
import 'pay_items_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// Loans and advances (contract 17.3): list, create with a local schedule
/// preview, detail with the instalments and the actions.
class LoansSection extends ConsumerStatefulWidget {
  const LoansSection({required this.canManage, super.key});
  final bool canManage;

  @override
  ConsumerState<LoansSection> createState() => _LoansSectionState();
}

class _LoansSectionState extends ConsumerState<LoansSection> {
  String? _status = 'active';
  String? _kind;

  LoanQuery get _query => LoanQuery(status: _status, kind: _kind);

  Future<void> _create() async {
    final settings = ref.read(payrollSettingsProvider).valueOrNull;
    final tenure = settings?['payroll.loan_max_tenure_months'];
    final r = await showDialog<LoanFormResult>(
      context: context,
      builder: (_) => LoanFormDialog(
        thisMonth: monthKey(ref.read(payrollClockProvider)()),
        maxTenure: tenure is num ? tenure.toInt() : defaultLoanMaxTenure,
      ),
    );
    if (r == null || !mounted) return;
    final repo = ref.read(payrollRepositoryProvider);
    final ok = await payGuard(context, () async {
      Loan? loan;
      final done = await runWithReauth(
        context,
        reason: 'Adding a ${r.kind} for ${r.user.fullName} needs your password.',
        action: (pw) async => loan = await repo.createLoan(
          userId: r.user.id,
          kind: r.kind,
          principal: r.principal,
          emi: r.emi,
          startPeriod: r.startPeriod,
          reason: r.reason,
          currentPassword: pw,
        ),
      );
      if (done && mounted) payrollSnack(context, '${loan?.kindLabel ?? 'Loan'} ${loan?.reference ?? ''} added.');
      return done;
    });
    if (ok) ref.invalidate(loansProvider);
  }

  @override
  Widget build(BuildContext context) {
    final q = _query;
    final async = ref.watch(loansProvider(q));
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
                width: 150,
                child: DropdownButtonFormField<String?>(
                  key: const ValueKey('loans-status'),
                  initialValue: _status,
                  isExpanded: true,
                  decoration: const InputDecoration(
                    labelText: 'Status',
                    isDense: true,
                    border: OutlineInputBorder(),
                  ),
                  items: const [
                    DropdownMenuItem<String?>(child: Text('All')),
                    DropdownMenuItem<String?>(value: 'active', child: Text('Active')),
                    DropdownMenuItem<String?>(value: 'closed', child: Text('Closed')),
                    DropdownMenuItem<String?>(value: 'cancelled', child: Text('Cancelled')),
                  ],
                  onChanged: (v) => setState(() => _status = v),
                ),
              ),
              SizedBox(
                width: 150,
                child: DropdownButtonFormField<String?>(
                  key: const ValueKey('loans-kind'),
                  initialValue: _kind,
                  isExpanded: true,
                  decoration: const InputDecoration(
                    labelText: 'Kind',
                    isDense: true,
                    border: OutlineInputBorder(),
                  ),
                  items: const [
                    DropdownMenuItem<String?>(child: Text('All')),
                    DropdownMenuItem<String?>(value: 'loan', child: Text('Loan')),
                    DropdownMenuItem<String?>(value: 'advance', child: Text('Advance')),
                  ],
                  onChanged: (v) => setState(() => _kind = v),
                ),
              ),
              if (widget.canManage)
                FilledButton.icon(
                  key: const ValueKey('loans-add'),
                  onPressed: _create,
                  icon: const Icon(Icons.add),
                  label: const Text('New loan'),
                ),
            ],
          ),
        ),
        Expanded(
          child: AdminPagedBody<Loan>(
            async: async,
            emptyMessage: 'No loans or advances match.',
            emptyIcon: Icons.account_balance_wallet_outlined,
            onRefresh: () async => ref.read(loansProvider(q).notifier).reload(),
            onLoadMore: () => ref.read(loansProvider(q).notifier).loadMore(),
            itemBuilder: (context, l) => Card(
              key: ValueKey('loan-${l.id}'),
              margin: const EdgeInsets.symmetric(vertical: 4),
              child: ListTile(
                title: Text('${l.kindLabel} ${l.reference}  -  ${l.user.fullName}'),
                subtitle: Text(
                  'Outstanding ${formatRupees(l.outstanding)} of ${formatRupees(l.principal)}\n'
                  '${l.nextInstallment == null ? 'No instalment due' : 'Next ${monthTitle(l.nextInstallment!.period)}: ${formatRupees(l.nextInstallment!.amount)}'}',
                ),
                isThreeLine: true,
                trailing: PayStatusChip(payItemStatusLabel(l.status), tone: payToneFor(l.status)),
                onTap: () async {
                  await Navigator.of(context).push<void>(
                    MaterialPageRoute<void>(builder: (_) => LoanDetailScreen(loanId: l.id)),
                  );
                  ref.invalidate(loansProvider);
                },
              ),
            ),
          ),
        ),
      ],
    );
  }
}

class LoanFormResult {
  const LoanFormResult({
    required this.user,
    required this.kind,
    required this.principal,
    required this.emi,
    required this.startPeriod,
    required this.reason,
  });
  final EmployeeRef user;
  final String kind;
  final String principal;
  final String emi;
  final String startPeriod;
  final String reason;
}

/// Create form with a live schedule preview. The server stays authoritative.
class LoanFormDialog extends StatefulWidget {
  const LoanFormDialog({required this.thisMonth, super.key, this.maxTenure = defaultLoanMaxTenure});
  final String thisMonth;
  final int maxTenure;

  @override
  State<LoanFormDialog> createState() => _LoanFormDialogState();
}

class _LoanFormDialogState extends State<LoanFormDialog> {
  final _form = GlobalKey<FormState>();
  final _principal = TextEditingController();
  final _emi = TextEditingController();
  final _reason = TextEditingController();
  late String _start = shiftMonth(widget.thisMonth, 1);
  String _kind = 'loan';
  EmployeeRef? _user;

  @override
  void dispose() {
    _principal.dispose();
    _emi.dispose();
    _reason.dispose();
    super.dispose();
  }

  Map<String, String> get _errors => validateLoanInput(
        principal: _principal.text,
        emi: _emi.text,
        startPeriod: _start,
        reason: _reason.text,
        maxTenure: widget.maxTenure,
      );

  void _submit() {
    if (!(_form.currentState?.validate() ?? false)) return;
    if (_errors.isNotEmpty || _user == null) return;
    Navigator.pop(
      context,
      LoanFormResult(
        user: _user!,
        kind: _kind,
        principal: normalizeMoney(_principal.text)!,
        emi: normalizeMoney(_emi.text)!,
        startPeriod: _start,
        reason: _reason.text.trim(),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final errs = _errors;
    final preview = errs['principal'] == null && errs['emi'] == null
        ? previewLoanSchedule(_principal.text, _emi.text, _start)
        : const <({String period, String amount})>[];
    return AlertDialog(
      title: const Text('New loan or advance'),
      content: SizedBox(
        width: 420,
        child: SingleChildScrollView(
          child: Form(
            key: _form,
            autovalidateMode: AutovalidateMode.onUserInteraction,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                EmployeePickerField(key: const ValueKey('loan-employee'), onPicked: (u) => _user = u),
                const SizedBox(height: 12),
                DropdownButtonFormField<String>(
                  key: const ValueKey('loan-kind'),
                  initialValue: _kind,
                  decoration: const InputDecoration(labelText: 'Kind', border: OutlineInputBorder()),
                  items: const [
                    DropdownMenuItem(value: 'loan', child: Text('Loan')),
                    DropdownMenuItem(value: 'advance', child: Text('Salary advance')),
                  ],
                  onChanged: (v) => setState(() => _kind = v ?? _kind),
                ),
                const SizedBox(height: 12),
                MoneyField(
                  key: const ValueKey('loan-principal'),
                  controller: _principal,
                  label: 'Principal',
                  positive: true,
                  onChanged: () => setState(() {}),
                ),
                const SizedBox(height: 12),
                MoneyField(
                  key: const ValueKey('loan-emi'),
                  controller: _emi,
                  label: 'Monthly instalment (EMI)',
                  positive: true,
                  onChanged: () => setState(() {}),
                ),
                if (errs['emi'] != null && _emi.text.isNotEmpty)
                  Align(
                    alignment: Alignment.centerLeft,
                    child: Text(
                      errs['emi']!,
                      key: const ValueKey('loan-emi-error'),
                      style: TextStyle(color: Theme.of(context).colorScheme.error),
                    ),
                  ),
                const SizedBox(height: 12),
                MonthDropdown(
                  label: 'First deduction month',
                  value: _start,
                  options: monthOptions(widget.thisMonth, back: 1),
                  onChanged: (v) => setState(() => _start = v ?? _start),
                ),
                const SizedBox(height: 12),
                TextFormField(
                  key: const ValueKey('loan-reason'),
                  controller: _reason,
                  maxLength: 200,
                  maxLines: 2,
                  minLines: 1,
                  onChanged: (_) => setState(() {}),
                  decoration: const InputDecoration(labelText: 'Reason', border: OutlineInputBorder()),
                  validator: (v) => errs['reason'],
                ),
                if (preview.isNotEmpty)
                  Align(
                    alignment: Alignment.centerLeft,
                    child: Column(
                      key: const ValueKey('loan-preview'),
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          '${preview.length} instalments, '
                          '${monthTitle(preview.first.period)} to ${monthTitle(preview.last.period)}',
                          style: Theme.of(context).textTheme.titleSmall,
                        ),
                        Text(
                          'Last instalment ${formatRupees(preview.last.amount)}. '
                          'Preview only: the server sets the final schedule.',
                          style: Theme.of(context).textTheme.bodySmall,
                        ),
                      ],
                    ),
                  ),
              ],
            ),
          ),
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(
          key: const ValueKey('loan-save'),
          onPressed: _submit,
          style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
          child: const Text('Continue'),
        ),
      ],
    );
  }
}

/// One loan: summary, instalments and the actions (skip, reschedule,
/// foreclose, cancel). Password-protected actions ask for your own password.
class LoanDetailScreen extends ConsumerStatefulWidget {
  const LoanDetailScreen({required this.loanId, super.key});
  final String loanId;

  @override
  ConsumerState<LoanDetailScreen> createState() => _LoanDetailScreenState();
}

class _LoanDetailScreenState extends ConsumerState<LoanDetailScreen> {
  Future<void> _act(
    Loan loan,
    Future<Loan> Function(PayrollRepository repo, String password) call, {
    required String okMessage,
    String? reauthReason,
  }) async {
    final repo = ref.read(payrollRepositoryProvider);
    await payGuard(context, () async {
      var done = true;
      if (reauthReason == null) {
        await call(repo, '');
      } else {
        done = await runWithReauth(
          context,
          reason: reauthReason,
          action: (pw) async => call(repo, pw),
        );
      }
      if (done && mounted) payrollSnack(context, okMessage);
      return done;
    });
    // A version conflict or a lock means the loan changed: always reload.
    ref.invalidate(loanDetailProvider(widget.loanId));
    ref.invalidate(loansProvider);
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    final async = ref.watch(loanDetailProvider(widget.loanId));
    final self = ref.watch(adminSelfIdProvider);
    final canManage = canManagePayroll(perms);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: Text(async.valueOrNull == null ? 'Loan' : '${async.valueOrNull!.kindLabel} ${async.valueOrNull!.reference}')),
        body: payAsync<Loan>(
          context,
          async,
          onRetry: () => ref.invalidate(loanDetailProvider(widget.loanId)),
          emptyMessage: 'Not found.',
          isEmpty: (_) => false,
          builder: (l) => RefreshIndicator(
            onRefresh: () async {
              ref.invalidate(loanDetailProvider(widget.loanId));
              await ref.read(loanDetailProvider(widget.loanId).future).then((_) {}, onError: (Object _) {});
            },
            child: _body(l, canManage && l.user.id != self),
          ),
        ),
      ),
    );
  }

  Widget _body(Loan l, bool canAct) {
    final theme = Theme.of(context);
    final actions = canAct && l.isActive;
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 48),
      children: [
        Row(
          children: [
            Expanded(child: Text(l.user.display, style: theme.textTheme.titleMedium)),
            PayStatusChip(payItemStatusLabel(l.status), tone: payToneFor(l.status)),
          ],
        ),
        if (l.closeMode != null) Text(loanCloseModeLabels[l.closeMode] ?? l.closeMode!),
        if (l.reason != null) Text('Reason: ${l.reason}'),
        SectionCard(
          title: 'Amounts',
          children: [
            AmountRow(label: 'Principal', value: formatRupees(l.principal)),
            AmountRow(label: 'Monthly instalment', value: formatRupees(l.emi)),
            AmountRow(label: 'Recovered', value: formatRupees(l.recovered)),
            AmountRow(label: 'In the current payslip', value: formatRupees(l.inProcess)),
            AmountRow(label: 'Outstanding', value: formatRupees(l.outstanding), bold: true),
          ],
        ),
        SectionCard(
          title: 'Instalments (${l.installments.length})',
          children: [
            for (final i in l.installments)
              Padding(
                key: ValueKey('inst-${i.id}'),
                padding: const EdgeInsets.symmetric(vertical: 4),
                child: Row(
                  children: [
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            '${i.hasLiveNumber ? '${i.seq}. ' : ''}${monthTitle(i.period)}'
                            '${i.kind == 'foreclosure' ? '  (foreclosure)' : ''}',
                          ),
                          Text(
                            '${formatRupees(i.amount)}  -  ${payItemStatusLabel(i.status)}',
                            style: theme.textTheme.bodySmall,
                          ),
                          if (i.flags.isNotEmpty) payNotes(context, i.flags.map(payItemFlagLabel)),
                        ],
                      ),
                    ),
                    if (actions && i.isPendingEmi)
                      TextButton(
                        key: ValueKey('skip-${i.id}'),
                        onPressed: () => _skip(l, i),
                        child: const Text('Skip'),
                      ),
                  ],
                ),
              ),
          ],
        ),
        if (actions) ...[
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              OutlinedButton(
                key: const ValueKey('loan-reschedule'),
                onPressed: () => _reschedule(l),
                child: const Text('Reschedule'),
              ),
              OutlinedButton(
                key: const ValueKey('loan-foreclose'),
                onPressed: () => _foreclose(l),
                child: const Text('Foreclose'),
              ),
              OutlinedButton(
                key: const ValueKey('loan-cancel'),
                onPressed: () => _cancel(l),
                child: const Text('Cancel loan'),
              ),
            ],
          ),
        ],
      ],
    );
  }

  Future<void> _skip(Loan l, LoanInstallment i) async {
    final reason = await showReasonDialog(
      context,
      title: 'Skip ${monthTitle(i.period)}?',
      hint: 'Reason (3 to 200 characters)',
      confirmLabel: 'Skip',
    );
    if (reason == null || !mounted) return;
    await _act(
      l,
      (repo, _) => repo.skipInstallment(l.id, installmentId: i.id, reason: reason, expectedVersion: l.version),
      okMessage: 'Instalment skipped. One was added after the last instalment.',
    );
  }

  Future<void> _reschedule(Loan l) async {
    final firstPending = l.installments.where((i) => i.isPendingEmi).map((i) => i.period).toList();
    final thisMonth = monthKey(ref.read(payrollClockProvider)());
    final from = firstPending.isEmpty ? thisMonth : firstPending.first;
    final r = await showDialog<_RescheduleResult>(
      context: context,
      builder: (_) => _RescheduleDialog(loan: l, options: monthOptionsWith(from, null, back: 0, ahead: 24)),
    );
    if (r == null || !mounted) return;
    await _act(
      l,
      (repo, pw) => repo.rescheduleLoan(
        l.id,
        emi: r.emi,
        fromPeriod: r.fromPeriod,
        reason: r.reason,
        expectedVersion: l.version,
        currentPassword: pw,
      ),
      okMessage: 'Schedule changed.',
      reauthReason: 'Changing the schedule of ${l.reference} needs your password.',
    );
  }

  Future<void> _foreclose(Loan l) async {
    final now = ref.read(payrollClockProvider)();
    final r = await showDialog<_ForecloseResult>(
      context: context,
      builder: (_) => _ForecloseDialog(
        loan: l,
        today: DateTime(now.year, now.month, now.day),
        months: monthOptions(monthKey(now), back: 0, ahead: 12),
      ),
    );
    if (r == null || !mounted) return;
    await _act(
      l,
      (repo, pw) => repo.forecloseLoan(
        l.id,
        mode: r.mode,
        period: r.period,
        paidOn: r.paidOn,
        reference: r.reference,
        reason: r.reason,
        expectedVersion: l.version,
        currentPassword: pw,
      ),
      okMessage: r.mode == 'payroll' ? 'Foreclosure scheduled in payroll.' : 'Loan closed.',
      reauthReason: 'Foreclosing ${l.reference} needs your password.',
    );
  }

  Future<void> _cancel(Loan l) async {
    final reason = await showReasonDialog(
      context,
      title: 'Cancel ${l.reference}?',
      hint: 'Reason (3 to 200 characters)',
      confirmLabel: 'Cancel loan',
    );
    if (reason == null || !mounted) return;
    await _act(
      l,
      (repo, pw) => repo.cancelLoan(
        l.id,
        reason: reason,
        expectedVersion: l.version,
        currentPassword: pw,
      ),
      okMessage: 'Loan cancelled.',
      reauthReason: 'Cancelling ${l.reference} needs your password.',
    );
  }

}

class _RescheduleResult {
  const _RescheduleResult(this.emi, this.fromPeriod, this.reason);
  final String emi;
  final String fromPeriod;
  final String reason;
}

class _RescheduleDialog extends StatefulWidget {
  const _RescheduleDialog({required this.loan, required this.options});
  final Loan loan;
  final List<String> options;

  @override
  State<_RescheduleDialog> createState() => _RescheduleDialogState();
}

class _RescheduleDialogState extends State<_RescheduleDialog> {
  final _form = GlobalKey<FormState>();
  final _emi = TextEditingController();
  final _reason = TextEditingController();
  late String _from = widget.options.first;

  @override
  void dispose() {
    _emi.dispose();
    _reason.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: const Text('Reschedule'),
        content: SingleChildScrollView(
          child: Form(
            key: _form,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Text(
                  'Pending instalments from the chosen month are replaced by new ones '
                  'of the amount below.',
                ),
                const SizedBox(height: 12),
                MoneyField(
                  key: const ValueKey('resched-emi'),
                  controller: _emi,
                  label: 'New monthly instalment',
                  positive: true,
                  helper: 'Current ${formatRupees(widget.loan.emi)}',
                ),
                const SizedBox(height: 12),
                MonthDropdown(
                  label: 'Starting from',
                  value: _from,
                  options: widget.options,
                  onChanged: (v) => setState(() => _from = v ?? _from),
                ),
                const SizedBox(height: 12),
                TextFormField(
                  key: const ValueKey('resched-reason'),
                  controller: _reason,
                  maxLength: 200,
                  decoration: const InputDecoration(labelText: 'Reason', border: OutlineInputBorder()),
                  validator: (v) => validateLopReason(v, overrideSet: true),
                ),
              ],
            ),
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            key: const ValueKey('resched-continue'),
            onPressed: () {
              if (_form.currentState?.validate() ?? false) {
                Navigator.pop(
                  context,
                  _RescheduleResult(normalizeMoney(_emi.text)!, _from, _reason.text.trim()),
                );
              }
            },
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: const Text('Continue'),
          ),
        ],
      );
}

class _ForecloseResult {
  const _ForecloseResult({
    required this.mode,
    required this.reason,
    this.period,
    this.paidOn,
    this.reference,
  });
  final String mode;
  final String reason;
  final String? period;
  final String? paidOn;
  final String? reference;
}

class _ForecloseDialog extends StatefulWidget {
  const _ForecloseDialog({required this.loan, required this.today, required this.months});
  final Loan loan;
  final DateTime today;
  final List<String> months;

  @override
  State<_ForecloseDialog> createState() => _ForecloseDialogState();
}

class _ForecloseDialogState extends State<_ForecloseDialog> {
  final _form = GlobalKey<FormState>();
  final _reason = TextEditingController();
  final _ref = TextEditingController();
  late final _paidOn = TextEditingController(text: ymd(widget.today));
  late String _period = widget.months.first;
  String _mode = 'payroll';

  @override
  void dispose() {
    _reason.dispose();
    _ref.dispose();
    _paidOn.dispose();
    super.dispose();
  }

  String? _validateDate(String? v) {
    final d = parseYmd((v ?? '').trim());
    if (d == null) return 'Enter a date like 2026-11-07';
    if (d.isAfter(widget.today)) return 'Not in the future';
    return null;
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: const Text('Foreclose'),
        content: SingleChildScrollView(
          child: Form(
            key: _form,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Outstanding ${formatRupees(widget.loan.outstanding)}'),
                const SizedBox(height: 8),
                SegmentedButton<String>(
                  key: const ValueKey('foreclose-mode'),
                  segments: const [
                    ButtonSegment(value: 'payroll', label: Text('Through payroll')),
                    ButtonSegment(value: 'outside', label: Text('Paid outside')),
                  ],
                  selected: {_mode},
                  onSelectionChanged: (s) => setState(() => _mode = s.first),
                ),
                const SizedBox(height: 12),
                if (_mode == 'payroll')
                  MonthDropdown(
                    label: 'Deduct the balance in',
                    value: _period,
                    options: widget.months,
                    onChanged: (v) => setState(() => _period = v ?? _period),
                  )
                else ...[
                  TextFormField(
                    key: const ValueKey('foreclose-date'),
                    controller: _paidOn,
                    keyboardType: TextInputType.datetime,
                    decoration: const InputDecoration(
                      labelText: 'Paid on (YYYY-MM-DD)',
                      border: OutlineInputBorder(),
                    ),
                    validator: _validateDate,
                  ),
                  const SizedBox(height: 12),
                  TextFormField(
                    key: const ValueKey('foreclose-reference'),
                    controller: _ref,
                    maxLength: 60,
                    decoration: const InputDecoration(
                      labelText: 'Payment reference',
                      border: OutlineInputBorder(),
                    ),
                    validator: (v) {
                      final t = (v ?? '').trim();
                      if (t.isEmpty) return 'Reference is required';
                      if (!loanReferenceRe.hasMatch(t)) return 'Letters, numbers, space and . _ / - only';
                      return null;
                    },
                  ),
                ],
                const SizedBox(height: 12),
                TextFormField(
                  key: const ValueKey('foreclose-reason'),
                  controller: _reason,
                  maxLength: 200,
                  decoration: const InputDecoration(labelText: 'Reason', border: OutlineInputBorder()),
                  validator: (v) => validateLopReason(v, overrideSet: true),
                ),
              ],
            ),
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            key: const ValueKey('foreclose-continue'),
            onPressed: () {
              if (!(_form.currentState?.validate() ?? false)) return;
              Navigator.pop(
                context,
                _ForecloseResult(
                  mode: _mode,
                  reason: _reason.text.trim(),
                  period: _mode == 'payroll' ? _period : null,
                  paidOn: _mode == 'outside' ? _paidOn.text.trim() : null,
                  reference: _mode == 'outside' ? _ref.text.trim() : null,
                ),
              );
            },
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: const Text('Continue'),
          ),
        ],
      );
}
