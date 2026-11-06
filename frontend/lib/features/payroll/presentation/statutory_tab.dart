import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart' show adminPermissionsProvider, runWithReauth;
import '../data/payroll_repository.dart';
import '../domain/payroll_models.dart';
import '../domain/payroll_money.dart';
import '../domain/phase5c_models.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

const _files = <(String, String)>[
  ('pf_ecr', 'PF ECR file'),
  ('esi', 'ESI contribution file'),
  ('pt', 'Professional tax file'),
];

/// Statutory tab (plan 14 phase 2): per-month PF / ESI / PT / TDS dues with
/// paid / unpaid status, blockers (names and missing identifier type only, never
/// values), file downloads and mark paid / unpaid. Viewing and downloads need
/// `payroll.view_all`; marking paid needs `payroll.manage` (server enforces both).
class StatutoryTab extends ConsumerStatefulWidget {
  const StatutoryTab({super.key});

  @override
  ConsumerState<StatutoryTab> createState() => _StatutoryTabState();
}

class _StatutoryTabState extends ConsumerState<StatutoryTab> {
  late DateTime _month;
  late DateTime _thisMonth;

  @override
  void initState() {
    super.initState();
    final now = ref.read(payrollClockProvider)();
    _thisMonth = DateTime(now.year, now.month);
    _month = _thisMonth;
  }

  String get _period => monthKey(_month);

  bool get _isCurrent => _month == _thisMonth;

  void _shift(int d) => setState(() => _month = DateTime(_month.year, _month.month + d));

  Future<void> _download(String kind) async {
    final period = _period;
    await downloadPayrollFile(context, ref, (repo) async {
      // PF ECR and ESI files contain UAN / ESI IP numbers: the server wants the admin's own password.
      if (kind == 'pt') return repo.statutoryFile(kind, period);
      PayrollFile? file;
      final done = await runWithReauth(
        context,
        reason: 'This file contains employee identifiers and needs your password.',
        action: (pw) async => file = await repo.statutoryFile(kind, period, currentPassword: pw),
      );
      return done ? file : null;
    });
  }

  Future<void> _mark(StatutoryDueItem item, {required bool paid}) async {
    final period = _period;
    final repo = ref.read(payrollRepositoryProvider);
    final now = ref.read(payrollClockProvider)();
    final label = statutoryTypeLabels[item.type] ?? item.type;
    try {
      if (paid) {
        final form = await showDialog<_PaidForm>(
          context: context,
          builder: (_) => _PaidDialog(
            label: label,
            period: period,
            due: item.amount,
            today: now,
            isTds: item.type == 'tds',
          ),
        );
        if (form == null || !mounted) return;
        final done = await runWithReauth(
          context,
          reason: 'Marking $label as paid needs your password.',
          action: (pw) => repo.setChallan(
            type: item.type,
            period: period,
            paid: true,
            paidOn: form.paidOn,
            reference: form.reference,
            amount: form.amount,
            bsrCode: form.bsrCode,
            challanSerial: form.challanSerial,
            depositedOn: form.depositedOn,
            currentPassword: pw,
          ),
        );
        if (done && mounted) payrollSnack(context, '$label marked as paid.');
      } else {
        final ok = await showDialog<bool>(
          context: context,
          builder: (ctx) => AlertDialog(
            title: Text('Mark $label as unpaid?'),
            content: const Text('The paid date and reference will be cleared.'),
            actions: [
              TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
              FilledButton(
                key: const ValueKey('unpaid-confirm'),
                onPressed: () => Navigator.pop(ctx, true),
                child: const Text('Mark unpaid'),
              ),
            ],
          ),
        );
        if (ok != true || !mounted) return;
        final done = await runWithReauth(
          context,
          reason: 'Marking $label as unpaid needs your password.',
          action: (pw) => repo.setChallan(
            type: item.type,
            period: period,
            paid: false,
            currentPassword: pw,
          ),
        );
        if (done && mounted) payrollSnack(context, '$label marked as unpaid.');
      }
      ref.invalidate(statutoryDuesProvider(period));
    } on ApiException catch (e) {
      if (!mounted) return;
      payrollSnack(context, p5cMsg(e));
      // The state may have changed under us (already paid / not paid).
      ref.invalidate(statutoryDuesProvider(period));
    }
  }

  @override
  Widget build(BuildContext context) {
    final period = _period;
    final async = ref.watch(statutoryDuesProvider(period));
    final canManage = canManagePayroll(ref.watch(adminPermissionsProvider));
    return Column(
      children: [
        Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            IconButton(
              key: const ValueKey('stat-prev'),
              tooltip: 'Previous month',
              icon: const Icon(Icons.chevron_left),
              onPressed: () => _shift(-1),
            ),
            Text(periodTitle(period), style: Theme.of(context).textTheme.titleMedium),
            IconButton(
              key: const ValueKey('stat-next'),
              tooltip: 'Next month',
              icon: const Icon(Icons.chevron_right),
              onPressed: _isCurrent ? null : () => _shift(1),
            ),
          ],
        ),
        Expanded(
          child: async.when(
            skipLoadingOnReload: true,
            loading: () => const LoadingView(label: 'Loading statutory dues'),
            error: (e, _) => e is ApiException && e.code == 'NOT_FOUND'
                ? const EmptyView(message: 'No payroll run for this month.', icon: Icons.event_busy_outlined)
                : ErrorView(
                    message: payrollMsg(e),
                    onRetry: () => ref.invalidate(statutoryDuesProvider(period)),
                  ),
            data: (months) {
              final m = months.isEmpty ? null : months.last;
              return RefreshIndicator(
                onRefresh: () async => ref.invalidate(statutoryDuesProvider(period)),
                child: ListView(
                  padding: const EdgeInsets.all(16),
                  children: m == null
                      ? const [Text('No statutory data for this month.')]
                      : _body(m, canManage),
                ),
              );
            },
          ),
        ),
      ],
    );
  }

  List<Widget> _body(StatutoryMonth m, bool canManage) {
    if (!m.isPublished) {
      return [
        Card(
          key: const ValueKey('stat-not-published'),
          child: ListTile(
            leading: const Icon(Icons.info_outline),
            title: Text(
              m.runStatus == null
                  ? 'No payroll run for this month.'
                  : 'The ${payrollStatusLabel(m.runStatus!).toLowerCase()} run is not published yet.',
            ),
            subtitle: const Text('Statutory dues and files use published payslips only.'),
          ),
        ),
      ];
    }
    return [
      if (m.items.isEmpty) const Padding(padding: EdgeInsets.all(12), child: Text('No dues this month.')),
      for (final i in m.items) _DueCard(item: i, canManage: canManage, onMark: _mark),
      const SizedBox(height: 8),
      _blockers(m),
      const SizedBox(height: 8),
      Card(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Padding(padding: EdgeInsets.all(12), child: Text('Files')),
            for (final f in _files)
              ListTile(
                key: ValueKey('stat-dl-${f.$1}'),
                dense: true,
                leading: const Icon(Icons.download_outlined),
                title: Text(f.$2),
                onTap: () => _download(f.$1),
              ),
            const Padding(
              padding: EdgeInsets.fromLTRB(16, 4, 16, 12),
              child: Text(
                'Check the file layouts against the current EPFO and ESIC portals before filing.',
              ),
            ),
          ],
        ),
      ),
    ];
  }

  Widget _blockers(StatutoryMonth m) {
    final cs = Theme.of(context).colorScheme;
    if (m.blockers.isEmpty) {
      return const Card(
        key: ValueKey('stat-no-blockers'),
        child: ListTile(
          leading: Icon(Icons.check_circle_outline),
          title: Text('No missing identifiers'),
        ),
      );
    }
    return Card(
      key: const ValueKey('stat-blockers'),
      color: cs.errorContainer,
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'Blockers (${m.blockers.length})',
              style: Theme.of(context).textTheme.titleSmall,
            ),
            const Text('PF and ESI files cannot be made until these are fixed in the employee payroll details. Payroll does not need to be run again.'),
            const SizedBox(height: 4),
            for (final b in m.blockers)
              ListTile(
                dense: true,
                contentPadding: EdgeInsets.zero,
                title: Text('${b.name}  -  ${b.employeeId}'),
                subtitle: Text(
                  b.missing
                      .map((x) => '${statutoryFieldLabel(x.field)} ${x.reason == 'invalid' ? 'invalid' : 'missing'}')
                      .join(', '),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _DueCard extends StatelessWidget {
  const _DueCard({required this.item, required this.canManage, required this.onMark});
  final StatutoryDueItem item;
  final bool canManage;
  final Future<void> Function(StatutoryDueItem item, {required bool paid}) onMark;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final cs = theme.colorScheme;
    final label = statutoryTypeLabels[item.type] ?? item.type;
    return Card(
      key: ValueKey('due-${item.type}'),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(child: Text(label, style: theme.textTheme.titleSmall)),
                Chip(
                  label: Text(item.paid ? 'Paid' : 'Unpaid'),
                  backgroundColor: item.paid ? cs.primaryContainer : cs.errorContainer,
                  labelStyle: TextStyle(color: item.paid ? cs.onPrimaryContainer : cs.onErrorContainer),
                  visualDensity: VisualDensity.compact,
                  side: BorderSide.none,
                ),
              ],
            ),
            Text('Due ${formatRupees(item.amount)}  -  ${item.employees} employees'),
            if (item.paid)
              Text(
                'Paid ${item.paidOn ?? ''}'
                '${item.reference == null ? '' : '  -  Ref ${item.reference}'}'
                '${item.paidAmount == null ? '' : '  -  ${formatRupees(item.paidAmount)}'}',
                style: theme.textTheme.bodySmall,
              ),
            if (item.paid && item.type == 'tds' && (item.bsrCode != null || item.challanSerial != null))
              Text(
                key: const ValueKey('tds-challan-details'),
                [
                  if (item.bsrCode != null) 'BSR code ${item.bsrCode}',
                  if (item.challanSerial != null) 'Serial ${item.challanSerial}',
                  if (item.depositedOn != null) 'Deposited ${item.depositedOn}',
                ].join('  -  '),
                style: theme.textTheme.bodySmall,
              ),
            if (item.paid && item.type == 'tds' && item.bsrCode == null)
              Text(
                'The BSR code, serial number and deposit date are missing. Mark it unpaid and paid again '
                'with them: the 24Q file needs them.',
                key: const ValueKey('tds-challan-missing'),
                style: theme.textTheme.bodySmall?.copyWith(color: cs.error),
              ),
            if (item.mismatch)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Row(
                  children: [
                    Icon(Icons.warning_amber_outlined, size: 16, color: cs.error),
                    const SizedBox(width: 4),
                    const Expanded(child: Text('The amount paid differs from the amount due.')),
                  ],
                ),
              ),
            if (canManage)
              Align(
                alignment: Alignment.centerRight,
                child: item.paid
                    ? TextButton(
                        key: ValueKey('mark-unpaid-${item.type}'),
                        onPressed: () => onMark(item, paid: false),
                        child: const Text('Mark unpaid'),
                      )
                    : FilledButton.tonal(
                        key: ValueKey('mark-paid-${item.type}'),
                        onPressed: () => onMark(item, paid: true),
                        child: const Text('Mark paid'),
                      ),
              ),
          ],
        ),
      ),
    );
  }
}

class _PaidForm {
  const _PaidForm(this.paidOn, this.reference, this.amount, {this.bsrCode, this.challanSerial, this.depositedOn});
  final String paidOn;
  final String reference;
  final String? amount;
  final String? bsrCode;
  final String? challanSerial;
  final String? depositedOn;
}

final _referenceRe = RegExp(r'^[A-Za-z0-9 ._/-]{1,60}$');

/// Date (YYYY-MM-DD), reference, optional amount. The password is asked next.
class _PaidDialog extends StatefulWidget {
  const _PaidDialog({
    required this.label,
    required this.period,
    required this.due,
    required this.today,
    this.isTds = false,
  });

  /// TDS challans also need the BSR code, the challan serial number and the deposit date (19.16).
  final bool isTds;
  final String label;
  final String period;
  final String due;
  final DateTime today;

  @override
  State<_PaidDialog> createState() => _PaidDialogState();
}

class _PaidDialogState extends State<_PaidDialog> {
  final _form = GlobalKey<FormState>();
  final _date = TextEditingController();
  final _ref = TextEditingController();
  final _amount = TextEditingController();
  final _bsr = TextEditingController();
  final _serial = TextEditingController();
  final _deposit = TextEditingController();

  DateTime get _start => parseMonth(widget.period) ?? DateTime(widget.today.year, widget.today.month);

  DateTime get _today => DateTime(widget.today.year, widget.today.month, widget.today.day);

  @override
  void initState() {
    super.initState();
    if (!_today.isBefore(_start)) {
      _date.text = ymd(_today);
      _deposit.text = ymd(_today);
    }
  }

  @override
  void dispose() {
    _date.dispose();
    _ref.dispose();
    _amount.dispose();
    _bsr.dispose();
    _serial.dispose();
    _deposit.dispose();
    super.dispose();
  }

  String? _validateDate(String? v) {
    final d = parseYmd((v ?? '').trim());
    if (d == null) return 'Enter a date like 2026-11-07';
    if (d.isBefore(_start)) return 'Not before ${ymd(_start)}';
    if (d.isAfter(_today)) return 'Not in the future';
    return null;
  }

  Future<void> _pickDeposit() async {
    final cur = parseYmd(_deposit.text.trim());
    final ok = cur != null && !cur.isBefore(_start) && !cur.isAfter(_today);
    final picked = await showDatePicker(
      context: context,
      initialDate: ok ? cur : (_today.isBefore(_start) ? _start : _today),
      firstDate: _start,
      lastDate: _today.isBefore(_start) ? _start : _today,
    );
    if (picked != null) setState(() => _deposit.text = ymd(picked));
  }

  Future<void> _pick() async {
    final cur = parseYmd(_date.text.trim());
    final ok = cur != null && !cur.isBefore(_start) && !cur.isAfter(_today);
    final picked = await showDatePicker(
      context: context,
      initialDate: ok ? cur : (_today.isBefore(_start) ? _start : _today),
      firstDate: _start,
      lastDate: _today.isBefore(_start) ? _start : _today,
    );
    if (picked != null) setState(() => _date.text = ymd(picked));
  }

  void _submit() {
    if (!(_form.currentState?.validate() ?? false)) return;
    final a = _amount.text.trim();
    Navigator.pop(
      context,
      _PaidForm(
        _date.text.trim(),
        _ref.text.trim(),
        a.isEmpty ? null : a,
        bsrCode: widget.isTds ? _bsr.text.trim() : null,
        challanSerial: widget.isTds ? _serial.text.trim() : null,
        depositedOn: widget.isTds ? _deposit.text.trim() : null,
      ),
    );
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: Text('Mark ${widget.label} as paid'),
        content: SingleChildScrollView(
          child: Form(
            key: _form,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextFormField(
                  key: const ValueKey('paid-date'),
                  controller: _date,
                  keyboardType: TextInputType.datetime,
                  decoration: InputDecoration(
                    labelText: 'Paid on (YYYY-MM-DD)',
                    border: const OutlineInputBorder(),
                    suffixIcon: IconButton(
                      tooltip: 'Pick a date',
                      icon: const Icon(Icons.calendar_today_outlined),
                      onPressed: _pick,
                    ),
                  ),
                  validator: _validateDate,
                ),
                const SizedBox(height: 12),
                TextFormField(
                  key: const ValueKey('paid-reference'),
                  controller: _ref,
                  maxLength: 60,
                  decoration: const InputDecoration(
                    labelText: 'Challan / payment reference',
                    border: OutlineInputBorder(),
                  ),
                  validator: (v) {
                    final t = (v ?? '').trim();
                    if (t.isEmpty) return 'Reference is required';
                    if (!_referenceRe.hasMatch(t)) return 'Letters, numbers, space and . _ / - only';
                    return null;
                  },
                ),
                if (widget.isTds) ...[
                  const SizedBox(height: 4),
                  TextFormField(
                    key: const ValueKey('paid-bsr'),
                    controller: _bsr,
                    keyboardType: TextInputType.number,
                    maxLength: 7,
                    inputFormatters: [FilteringTextInputFormatter.digitsOnly],
                    decoration: const InputDecoration(
                      labelText: 'BSR code of the bank branch',
                      helperText: '7 digits, from the challan receipt',
                      border: OutlineInputBorder(),
                    ),
                    validator: validateBsrCode,
                  ),
                  const SizedBox(height: 4),
                  TextFormField(
                    key: const ValueKey('paid-serial'),
                    controller: _serial,
                    keyboardType: TextInputType.number,
                    maxLength: 5,
                    inputFormatters: [FilteringTextInputFormatter.digitsOnly],
                    decoration: const InputDecoration(
                      labelText: 'Challan serial number',
                      helperText: 'Up to 5 digits',
                      border: OutlineInputBorder(),
                    ),
                    validator: validateChallanSerial,
                  ),
                  const SizedBox(height: 4),
                  TextFormField(
                    key: const ValueKey('paid-deposit'),
                    controller: _deposit,
                    keyboardType: TextInputType.datetime,
                    decoration: InputDecoration(
                      labelText: 'Deposited on (YYYY-MM-DD)',
                      border: const OutlineInputBorder(),
                      suffixIcon: IconButton(
                        tooltip: 'Pick a date',
                        icon: const Icon(Icons.calendar_today_outlined),
                        onPressed: _pickDeposit,
                      ),
                    ),
                    validator: (v) => validateDepositedOn(v, widget.today, _start),
                  ),
                ],
                const SizedBox(height: 4),
                MoneyField(
                  key: const ValueKey('paid-amount'),
                  controller: _amount,
                  label: 'Amount paid (optional)',
                  required: false,
                  positive: true,
                  helper: 'Leave empty to use the amount due (${formatRupees(widget.due)})',
                ),
              ],
            ),
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            key: const ValueKey('paid-continue'),
            onPressed: _submit,
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: const Text('Continue'),
          ),
        ],
      );
}
