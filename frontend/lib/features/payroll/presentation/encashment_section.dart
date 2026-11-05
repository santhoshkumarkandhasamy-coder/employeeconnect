import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import '../../admin/presentation/admin_widgets.dart'
    show AdminPagedBody, adminSelfIdProvider, confirmAction, runWithReauth;
import '../data/payroll_repository.dart';
import '../domain/pay_items_models.dart';
import '../domain/payroll_logic.dart';
import '../domain/payroll_models.dart';
import '../domain/payroll_money.dart';
import 'pay_items_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

String _daysText(double d) => formatDays(d);

/// Leave encashment (contract 17.5): list, server quote, create (password),
/// cancel while pending.
class EncashmentSection extends ConsumerStatefulWidget {
  const EncashmentSection({required this.canManage, super.key});
  final bool canManage;

  @override
  ConsumerState<EncashmentSection> createState() => _EncashmentSectionState();
}

class _EncashmentSectionState extends ConsumerState<EncashmentSection> {
  late int _year = ref.read(payrollClockProvider)().year;
  String? _status;

  EncashmentQuery get _query => EncashmentQuery(year: _year, status: _status);

  Future<void> _create() async {
    final now = ref.read(payrollClockProvider)();
    final r = await showDialog<EncashFormResult>(
      context: context,
      builder: (_) => EncashFormDialog(now: now),
    );
    if (r == null || !mounted) return;
    final repo = ref.read(payrollRepositoryProvider);
    final ok = await payGuard(context, () async {
      final done = await runWithReauth(
        context,
        reason: 'Encashing leave for ${r.user.fullName} needs your password.',
        action: (pw) => repo.createEncashment(
          userId: r.user.id,
          leaveTypeId: r.leaveTypeId,
          year: r.year,
          days: r.days,
          period: r.period,
          reason: r.reason,
          currentPassword: pw,
        ),
      );
      if (done && mounted) {
        payrollSnack(context, 'Encashment added. The days were taken off the leave balance.');
      }
      return done;
    });
    if (ok) ref.invalidate(encashmentsProvider);
  }

  Future<void> _cancel(Encashment e) async {
    final ok = await confirmAction(
      context,
      title: 'Cancel this encashment?',
      message: 'The ${_daysText(e.days)} days go back to the leave balance.',
      confirmLabel: 'Cancel encashment',
    );
    if (!ok || !mounted) return;
    final repo = ref.read(payrollRepositoryProvider);
    await payGuard(context, () async {
      await repo.cancelEncashment(e.id);
      if (mounted) payrollSnack(context, 'Encashment cancelled.');
      return true;
    });
    ref.invalidate(encashmentsProvider);
  }

  @override
  Widget build(BuildContext context) {
    final q = _query;
    final async = ref.watch(encashmentsProvider(q));
    final now = ref.read(payrollClockProvider)();
    return Column(
      children: [
        YearSelector(year: _year, maxYear: now.year, onChanged: (y) => setState(() => _year = y)),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12),
          child: Wrap(
            spacing: 8,
            runSpacing: 8,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              SizedBox(
                width: 150,
                child: DropdownButtonFormField<String?>(
                  key: const ValueKey('enc-status'),
                  initialValue: _status,
                  isExpanded: true,
                  decoration: const InputDecoration(
                    labelText: 'Status',
                    isDense: true,
                    border: OutlineInputBorder(),
                  ),
                  items: const [
                    DropdownMenuItem<String?>(child: Text('All')),
                    DropdownMenuItem<String?>(value: 'pending', child: Text('Pending')),
                    DropdownMenuItem<String?>(value: 'processing', child: Text('In payslip')),
                    DropdownMenuItem<String?>(value: 'paid', child: Text('Paid')),
                    DropdownMenuItem<String?>(value: 'cancelled', child: Text('Cancelled')),
                  ],
                  onChanged: (v) => setState(() => _status = v),
                ),
              ),
              if (widget.canManage)
                FilledButton.icon(
                  key: const ValueKey('enc-add'),
                  onPressed: _create,
                  icon: const Icon(Icons.add),
                  label: const Text('New encashment'),
                ),
            ],
          ),
        ),
        Expanded(
          child: AdminPagedBody<Encashment>(
            async: async,
            emptyMessage: 'No leave encashments for $_year.',
            emptyIcon: Icons.event_available_outlined,
            onRefresh: () async => ref.read(encashmentsProvider(q).notifier).reload(),
            onLoadMore: () => ref.read(encashmentsProvider(q).notifier).loadMore(),
            itemBuilder: (context, e) => Card(
              key: ValueKey('enc-${e.id}'),
              margin: const EdgeInsets.symmetric(vertical: 4),
              child: Padding(
                padding: const EdgeInsets.fromLTRB(14, 10, 6, 10),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(e.user.display, style: Theme.of(context).textTheme.titleSmall),
                          Text('${e.leaveType.name}: ${_daysText(e.days)} days for ${e.year}'),
                          Text(
                            '${formatRupees(e.amount)}  (${formatRupees(e.dailyRate)} a day)',
                            style: Theme.of(context).textTheme.titleMedium,
                          ),
                          Text('Paid in ${monthTitle(e.period)}'),
                          if (e.isPending && runStatusHint(e.runStatus) != null)
                            payNotes(context, [runStatusHint(e.runStatus)!]),
                          const SizedBox(height: 4),
                          PayStatusChip(payItemStatusLabel(e.status), tone: payToneFor(e.status)),
                        ],
                      ),
                    ),
                    if (widget.canManage && e.isPending && e.user.id != ref.read(adminSelfIdProvider))
                      IconButton(
                        key: ValueKey('enc-cancel-${e.id}'),
                        tooltip: 'Cancel encashment',
                        icon: const Icon(Icons.cancel_outlined),
                        onPressed: () => _cancel(e),
                      ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ],
    );
  }
}

class EncashFormResult {
  const EncashFormResult({
    required this.user,
    required this.leaveTypeId,
    required this.year,
    required this.days,
    required this.period,
    required this.reason,
  });
  final EmployeeRef user;
  final String leaveTypeId;
  final int year;
  final double days;
  final String period;
  final String reason;
}

/// Pick employee, leave type, year, month and days, then ask the server for a
/// quote (available days, daily rate, amount). The amount comes from the
/// server, never from this device.
class EncashFormDialog extends ConsumerStatefulWidget {
  const EncashFormDialog({required this.now, super.key});
  final DateTime now;

  @override
  ConsumerState<EncashFormDialog> createState() => _EncashFormDialogState();
}

class _EncashFormDialogState extends ConsumerState<EncashFormDialog> {
  final _form = GlobalKey<FormState>();
  final _days = TextEditingController();
  final _reason = TextEditingController();
  EmployeeRef? _user;
  String? _typeId;
  late int _year = widget.now.year;
  late String _period = monthKey(widget.now);
  EncashmentQuote? _quote;
  String? _quoteError;
  bool _quoting = false;

  @override
  void dispose() {
    _days.dispose();
    _reason.dispose();
    super.dispose();
  }

  void _invalidateQuote() => setState(() {
        _quote = null;
        _quoteError = null;
      });

  Future<void> _getQuote() async {
    final u = _user;
    final t = _typeId;
    if (u == null || t == null) {
      setState(() => _quoteError = 'Choose the employee and the leave type first.');
      return;
    }
    final text = _days.text.trim();
    if (text.isNotEmpty) {
      final bad = validateEncashDays(text);
      if (bad != null) {
        setState(() => _quoteError = bad);
        return;
      }
    }
    final repo = ref.read(payrollRepositoryProvider);
    setState(() {
      _quoting = true;
      _quoteError = null;
      _quote = null;
    });
    try {
      final q = await repo.encashmentQuote(
        u.id,
        leaveTypeId: t,
        year: _year,
        period: _period,
        days: text.isEmpty ? null : double.parse(text),
      );
      if (!mounted) return;
      setState(() {
        _quote = q;
        if (text.isEmpty) _days.text = _daysText(q.days);
      });
    } on ApiException catch (e) {
      if (mounted) setState(() => _quoteError = payrollErrorMessage(e));
    } finally {
      if (mounted) setState(() => _quoting = false);
    }
  }

  void _submit() {
    if (!(_form.currentState?.validate() ?? false)) return;
    final q = _quote;
    if (q == null || _user == null || _typeId == null) {
      setState(() => _quoteError = 'Get the quote first.');
      return;
    }
    Navigator.pop(
      context,
      EncashFormResult(
        user: _user!,
        leaveTypeId: _typeId!,
        year: _year,
        days: double.parse(_days.text.trim()),
        period: _period,
        reason: _reason.text.trim(),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final types = ref.watch(encashableLeaveTypesProvider);
    final q = _quote;
    final theme = Theme.of(context);
    return AlertDialog(
      title: const Text('New leave encashment'),
      content: SizedBox(
        width: 420,
        child: SingleChildScrollView(
          child: Form(
            key: _form,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                EmployeePickerField(
                  key: const ValueKey('enc-employee'),
                  onPicked: (u) {
                    _user = u;
                    _invalidateQuote();
                  },
                ),
                const SizedBox(height: 12),
                types.when(
                  loading: () => const LinearProgressIndicator(),
                  error: (e, _) => Text(payrollMsg(e)),
                  data: (list) => list.isEmpty
                      ? const Text(
                          'No leave type can be encashed. Turn on "Encashable" in a leave type first.',
                          key: ValueKey('enc-no-types'),
                        )
                      : DropdownButtonFormField<String>(
                          key: const ValueKey('enc-type'),
                          initialValue: _typeId,
                          isExpanded: true,
                          decoration: const InputDecoration(
                            labelText: 'Leave type',
                            border: OutlineInputBorder(),
                          ),
                          items: [
                            for (final t in list) DropdownMenuItem(value: t.id, child: Text(t.name)),
                          ],
                          validator: (v) => v == null ? 'Choose a leave type' : null,
                          onChanged: (v) {
                            _typeId = v;
                            _invalidateQuote();
                          },
                        ),
                ),
                const SizedBox(height: 12),
                DropdownButtonFormField<int>(
                  key: const ValueKey('enc-year'),
                  initialValue: _year,
                  decoration: const InputDecoration(labelText: 'Leave year', border: OutlineInputBorder()),
                  items: [
                    for (final y in [widget.now.year, widget.now.year - 1])
                      DropdownMenuItem(value: y, child: Text('$y')),
                  ],
                  onChanged: (v) {
                    _year = v ?? _year;
                    _invalidateQuote();
                  },
                ),
                const SizedBox(height: 12),
                MonthDropdown(
                  label: 'Paid in',
                  value: _period,
                  options: monthOptions(monthKey(widget.now), back: 1),
                  onChanged: (v) {
                    _period = v ?? _period;
                    _invalidateQuote();
                  },
                ),
                const SizedBox(height: 12),
                TextFormField(
                  key: const ValueKey('enc-days'),
                  controller: _days,
                  keyboardType: const TextInputType.numberWithOptions(decimal: true),
                  onChanged: (_) => _invalidateQuote(),
                  decoration: const InputDecoration(
                    labelText: 'Days to encash',
                    helperText: 'Steps of 0.5. Leave empty to use every available day.',
                    border: OutlineInputBorder(),
                  ),
                  validator: (v) => validateEncashDays(v, max: _quote?.availableDays),
                ),
                const SizedBox(height: 8),
                Align(
                  alignment: Alignment.centerLeft,
                  child: OutlinedButton(
                    key: const ValueKey('enc-quote'),
                    onPressed: _quoting ? null : _getQuote,
                    child: Text(_quoting ? 'Getting quote' : 'Get quote'),
                  ),
                ),
                if (_quoteError != null)
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: Text(
                      _quoteError!,
                      key: const ValueKey('enc-quote-error'),
                      style: TextStyle(color: theme.colorScheme.error),
                    ),
                  ),
                if (q != null)
                  Card(
                    key: const ValueKey('enc-quote-card'),
                    child: Padding(
                      padding: const EdgeInsets.all(10),
                      child: Column(
                        children: [
                          AmountRow(label: 'Available', value: '${_daysText(q.availableDays)} days'),
                          AmountRow(label: 'Daily rate (${q.basis} / ${q.divisor})', value: formatRupees(q.dailyRate)),
                          AmountRow(label: '${_daysText(q.days)} days', value: formatRupees(q.amount), bold: true),
                        ],
                      ),
                    ),
                  ),
                const SizedBox(height: 12),
                TextFormField(
                  key: const ValueKey('enc-reason'),
                  controller: _reason,
                  maxLength: 200,
                  decoration: const InputDecoration(labelText: 'Reason', border: OutlineInputBorder()),
                  validator: (v) => validateLopReason(v, overrideSet: true),
                ),
              ],
            ),
          ),
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(
          key: const ValueKey('enc-continue'),
          onPressed: _submit,
          style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
          child: const Text('Continue'),
        ),
      ],
    );
  }
}
