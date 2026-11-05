import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../data/payroll_repository.dart';
import '../domain/pay_items_models.dart' show LeaveTypeRef;
import '../domain/payroll_money.dart';
import '../domain/phase4b_models.dart';
import 'fnf_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

class _EncLine {
  _EncLine({required this.type, required this.year, required double days, this.available})
      : days = TextEditingController(text: formatDays1(days));
  final LeaveTypeRef type;
  final int year;
  final double? available;
  final TextEditingController days;
}

class _LoanLine {
  _LoanLine(FnfLoan l)
      : loan = l,
        mode = l.mode,
        paidOn = parseYmd(l.paidOn),
        reference = TextEditingController(text: l.paymentReference ?? '');
  final FnfLoan loan;
  String? mode;
  DateTime? paidOn;
  final TextEditingController reference;
}

class _ItemLine {
  _ItemLine({required this.code, String label = '', String amount = ''})
      : label = TextEditingController(text: label),
        amount = TextEditingController(text: amount);
  String code;
  final TextEditingController label;
  final TextEditingController amount;
}

/// Edits a DRAFT settlement: exit dates, notice (waive / override with a
/// reason), leave encashment, loan foreclosure, other items and the open-items
/// switch. Lists are sent whole (contract 18.8 PUT). Approved, settled and
/// submitted settlements are read-only here.
class FnfEditorScreen extends ConsumerStatefulWidget {
  const FnfEditorScreen({required this.fnf, super.key});
  final Fnf fnf;

  @override
  ConsumerState<FnfEditorScreen> createState() => _FnfEditorScreenState();
}

class _FnfEditorScreenState extends ConsumerState<FnfEditorScreen> {
  final _form = GlobalKey<FormState>();
  late ExitValues _exit;
  Map<String, String> _exitErrors = const {};
  late bool _waived = widget.fnf.notice.waived;
  late bool _override = widget.fnf.notice.hasOverride;
  late final _overrideDays = TextEditingController(text: '${widget.fnf.notice.overrideDays ?? widget.fnf.notice.computedShortfallDays}');
  late final _overrideReason = TextEditingController(text: widget.fnf.notice.overrideReason ?? '');
  late final List<_EncLine> _enc = [
    for (final e in widget.fnf.encashments)
      _EncLine(type: e.leaveType, year: e.year, days: e.days, available: e.availableDays),
  ];
  late final List<_LoanLine> _loans = [for (final l in widget.fnf.loans) _LoanLine(l)];
  late final List<_ItemLine> _items = [
    for (final i in widget.fnf.items) _ItemLine(code: i.code, label: i.label, amount: i.amount),
  ];
  late bool _move = widget.fnf.moveOpenItems;
  late final _remarks = TextEditingController(text: widget.fnf.remarks ?? '');
  bool _saving = false;

  Fnf get _f => widget.fnf;

  @override
  void initState() {
    super.initState();
    _exit = ExitValues(
      lastWorkingDay: parseYmd(_f.lastWorkingDay),
      resignationDate: parseYmd(_f.resignationDate),
      exitReason: _f.exitReason,
      settlementPeriod: _f.settlementPeriod,
    );
  }

  @override
  void dispose() {
    for (final c in [_overrideDays, _overrideReason, _remarks]) {
      c.dispose();
    }
    for (final e in _enc) {
      e.days.dispose();
    }
    for (final l in _loans) {
      l.reference.dispose();
    }
    for (final i in _items) {
      i.label.dispose();
      i.amount.dispose();
    }
    super.dispose();
  }

  Future<void> _addEncashment() async {
    final line = await showDialog<_EncLine>(
      context: context,
      builder: (_) => _AddEncashDialog(lwdYear: _exit.lastWorkingDay?.year ?? ref.read(payrollClockProvider)().year),
    );
    if (line != null && mounted) {
      if (_enc.any((e) => e.type.id == line.type.id && e.year == line.year)) {
        payrollSnack(context, 'That leave type and year is already listed.');
        return;
      }
      setState(() => _enc.add(line));
    }
  }

  Future<void> _save() async {
    if (_saving || !_f.isEditable) return;
    final today = ref.read(payrollClockProvider)();
    final errs = validateExit(_exit, today);
    final ok = _form.currentState?.validate() ?? false;
    setState(() => _exitErrors = errs);
    if (!ok || errs.isNotEmpty) return;
    final plan = validateFnfPlan(encashments: _enc.length, loans: _loans.length, items: _items.length);
    if (plan != null) {
      payrollSnack(context, plan);
      return;
    }
    for (final l in _loans) {
      if (l.mode == 'outside' && l.paidOn == null) {
        payrollSnack(context, 'Choose the date ${l.loan.reference} was paid.');
        return;
      }
    }
    final edit = FnfEdit(
      exit: _exit,
      waived: _waived,
      overrideDays: _override && !_waived ? int.parse(_overrideDays.text.trim()) : null,
      overrideReason: _overrideReason.text,
      clearOverride: !_override || _waived,
      encashments: [
        for (final e in _enc)
          FnfEncashmentInput(leaveTypeId: e.type.id, year: e.year, days: parseEncashDays(e.days.text)!),
      ],
      loans: [
        for (final l in _loans)
          if (l.mode != null)
            FnfLoanInput(
              loanId: l.loan.loanId,
              mode: l.mode!,
              paidOn: l.mode == 'outside' ? ymd(l.paidOn!) : null,
              reference: l.mode == 'outside' ? l.reference.text.trim() : null,
            ),
      ],
      items: [
        for (final i in _items)
          FnfItemInput(code: i.code, label: i.label.text, amount: normalizeMoney(i.amount.text)!),
      ],
      moveOpenItems: _move,
      remarks: _remarks.text,
    );
    setState(() => _saving = true);
    final repo = ref.read(payrollRepositoryProvider);
    final nav = Navigator.of(context);
    try {
      await repo.updateFnf(_f.id, edit, expectedVersion: _f.version);
      invalidateFnfState(ref, id: _f.id);
      nav.pop(true);
    } on ApiException catch (e) {
      if (isStaleFnfError(e)) invalidateFnfState(ref, id: _f.id);
      if (mounted) payrollSnack(context, fnfErrorMessage(e));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final f = _f;
    final theme = Theme.of(context);
    if (!f.isEditable) {
      return SecureScreen(
        child: Scaffold(
          appBar: AppBar(title: const Text('Edit settlement')),
          body: const Padding(
            padding: EdgeInsets.all(24),
            child: Text(
              'Only a draft can be edited. Withdraw a submitted settlement, or revoke an approved one, first.',
              key: ValueKey('editor-readonly'),
            ),
          ),
        ),
      );
    }
    final today = ref.read(payrollClockProvider)();
    const gap = SizedBox(height: 12);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: Text('Edit - ${f.user.display}')),
        body: Form(
          key: _form,
          child: ListView(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 48),
            children: [
              SectionCard(
                title: 'Exit details',
                children: [
                  ExitFields(initial: _exit, today: today, errors: _exitErrors, onChanged: (v) => _exit = v),
                ],
              ),
              SectionCard(
                title: 'Notice shortfall',
                children: [
                  Text(
                    'Required ${f.notice.requiredDays} days, served ${f.notice.servedDays ?? '-'}, '
                    'computed shortfall ${f.notice.computedShortfallDays} days. Only a resignation is recovered.',
                    style: theme.textTheme.bodySmall,
                  ),
                  SwitchListTile(
                    key: const ValueKey('notice-waive'),
                    contentPadding: EdgeInsets.zero,
                    title: const Text('Waive the notice recovery'),
                    value: _waived,
                    onChanged: (v) => setState(() => _waived = v),
                  ),
                  if (!_waived) ...[
                    SwitchListTile(
                      key: const ValueKey('notice-override-switch'),
                      contentPadding: EdgeInsets.zero,
                      title: const Text('Set the shortfall days myself'),
                      value: _override,
                      onChanged: (v) => setState(() => _override = v),
                    ),
                    if (_override) ...[
                      TextFormField(
                        key: const ValueKey('notice-override-days'),
                        controller: _overrideDays,
                        keyboardType: TextInputType.number,
                        decoration: InputDecoration(
                          labelText: 'Shortfall days (0 to ${_exit.noticePeriodDays ?? f.notice.requiredDays})',
                          border: const OutlineInputBorder(),
                        ),
                        validator: (v) {
                          final n = int.tryParse((v ?? '').trim());
                          final max = _exit.noticePeriodDays ?? f.notice.requiredDays;
                          if (n == null || n < 0 || n > max) return 'Enter a whole number from 0 to $max';
                          return null;
                        },
                      ),
                      gap,
                      TextFormField(
                        key: const ValueKey('notice-override-reason'),
                        controller: _overrideReason,
                        maxLength: 200,
                        decoration: const InputDecoration(labelText: 'Reason', border: OutlineInputBorder()),
                        validator: (v) => validateFnfReason(v),
                      ),
                    ],
                  ],
                ],
              ),
              SectionCard(
                title: 'Leave encashment',
                children: [
                  if (_enc.isEmpty) const Text('No leave is encashed.'),
                  for (var i = 0; i < _enc.length; i++)
                    Padding(
                      padding: const EdgeInsets.symmetric(vertical: 4),
                      child: Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Expanded(child: Text('${_enc[i].type.name} ${_enc[i].year}')),
                          SizedBox(
                            width: 110,
                            child: TextFormField(
                              key: ValueKey('enc-days-$i'),
                              controller: _enc[i].days,
                              keyboardType: const TextInputType.numberWithOptions(decimal: true),
                              decoration: const InputDecoration(labelText: 'Days', isDense: true, border: OutlineInputBorder()),
                              validator: (v) => validateEncashDays(v, available: _enc[i].available),
                            ),
                          ),
                          IconButton(
                            key: ValueKey('enc-remove-$i'),
                            tooltip: 'Remove',
                            icon: const Icon(Icons.delete_outline),
                            onPressed: () => setState(() => _enc.removeAt(i).days.dispose()),
                          ),
                        ],
                      ),
                    ),
                  Align(
                    alignment: Alignment.centerLeft,
                    child: TextButton.icon(
                      key: const ValueKey('enc-add'),
                      onPressed: _enc.length >= 10 ? null : _addEncashment,
                      icon: const Icon(Icons.add),
                      label: const Text('Add leave encashment'),
                    ),
                  ),
                ],
              ),
              SectionCard(
                title: 'Loans and advances',
                children: [
                  if (_loans.isEmpty) const Text('No active loans.'),
                  for (var i = 0; i < _loans.length; i++) _loanTile(i),
                ],
              ),
              SectionCard(
                title: 'Other items',
                children: [
                  if (_items.isEmpty) const Text('None.'),
                  for (var i = 0; i < _items.length; i++) _itemTile(i),
                  Align(
                    alignment: Alignment.centerLeft,
                    child: TextButton.icon(
                      key: const ValueKey('item-add'),
                      onPressed: _items.length >= 20
                          ? null
                          : () => setState(() => _items.add(_ItemLine(code: 'OTHER_EARNING'))),
                      icon: const Icon(Icons.add),
                      label: const Text('Add item'),
                    ),
                  ),
                ],
              ),
              SectionCard(
                title: 'Open items',
                children: [
                  SwitchListTile(
                    key: const ValueKey('move-open'),
                    contentPadding: EdgeInsets.zero,
                    title: const Text('Move unpaid items to the settlement month'),
                    subtitle: Text(
                      f.openItems.isEmpty
                          ? 'Nothing is open right now.'
                          : '${f.openItems.length} unpaid item(s) of other months would never be paid otherwise.',
                    ),
                    value: _move,
                    onChanged: (v) => setState(() => _move = v),
                  ),
                ],
              ),
              TextFormField(
                key: const ValueKey('editor-remarks'),
                controller: _remarks,
                maxLength: 200,
                maxLines: 2,
                minLines: 1,
                decoration: const InputDecoration(labelText: 'Remarks (optional)', border: OutlineInputBorder()),
                validator: (v) {
                  final t = (v ?? '').trim();
                  return t.isNotEmpty && t.length < 3 ? 'At least 3 characters' : null;
                },
              ),
              const SizedBox(height: 16),
              FilledButton(
                key: const ValueKey('editor-save'),
                style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
                onPressed: _saving ? null : _save,
                child: _saving
                    ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                    : const Text('Save'),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _loanTile(int i) {
    final l = _loans[i];
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          AmountRow(label: '${l.loan.kindLabel} ${l.loan.reference}', value: formatRupees(l.loan.outstanding), caption: 'Outstanding'),
          DropdownButtonFormField<String?>(
            key: ValueKey('loan-mode-$i'),
            initialValue: l.mode,
            isExpanded: true,
            decoration: const InputDecoration(labelText: 'How is it settled?', isDense: true, border: OutlineInputBorder()),
            items: const [
              DropdownMenuItem<String?>(child: Text('Not decided yet')),
              DropdownMenuItem<String?>(value: 'payroll', child: Text('Recover in the final payslip')),
              DropdownMenuItem<String?>(value: 'outside', child: Text('Paid outside payroll')),
            ],
            onChanged: (v) => setState(() => l.mode = v),
          ),
          if (l.mode == 'outside') ...[
            const SizedBox(height: 8),
            InkWell(
              key: ValueKey('loan-date-$i'),
              onTap: () async {
                final t = ref.read(payrollClockProvider)();
                final d = await showDatePicker(
                  context: context,
                  initialDate: l.paidOn ?? t,
                  firstDate: DateTime(t.year - 1),
                  lastDate: t,
                );
                if (d != null) setState(() => l.paidOn = d);
              },
              child: InputDecorator(
                decoration: const InputDecoration(labelText: 'Paid on', isDense: true, border: OutlineInputBorder()),
                child: Text(l.paidOn == null ? 'Tap to choose' : formatDayLong(l.paidOn!)),
              ),
            ),
            const SizedBox(height: 8),
            TextFormField(
              key: ValueKey('loan-ref-$i'),
              controller: l.reference,
              maxLength: 60,
              decoration: const InputDecoration(labelText: 'Payment reference', isDense: true, border: OutlineInputBorder()),
              validator: validatePaymentReference,
            ),
          ],
        ],
      ),
    );
  }

  Widget _itemTile(int i) {
    final it = _items[i];
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Column(
        children: [
          Row(
            children: [
              Expanded(
                child: DropdownButtonFormField<String>(
                  key: ValueKey('item-code-$i'),
                  initialValue: it.code,
                  isExpanded: true,
                  decoration: const InputDecoration(labelText: 'Type', isDense: true, border: OutlineInputBorder()),
                  items: [for (final c in fnfItemCodes) DropdownMenuItem(value: c, child: Text(fnfItemCodeLabel(c)))],
                  onChanged: (v) => setState(() => it.code = v ?? it.code),
                ),
              ),
              IconButton(
                key: ValueKey('item-remove-$i'),
                tooltip: 'Remove',
                icon: const Icon(Icons.delete_outline),
                onPressed: () => setState(() {
                  final r = _items.removeAt(i);
                  r.label.dispose();
                  r.amount.dispose();
                }),
              ),
            ],
          ),
          const SizedBox(height: 8),
          TextFormField(
            key: ValueKey('item-label-$i'),
            controller: it.label,
            maxLength: 40,
            decoration: const InputDecoration(labelText: 'Label', isDense: true, border: OutlineInputBorder()),
            validator: (v) => (v ?? '').trim().isEmpty ? 'A label is required (1 to 40 characters)' : null,
          ),
          MoneyField(key: ValueKey('item-amount-$i'), controller: it.amount, label: 'Amount', positive: true),
        ],
      ),
    );
  }
}

class _AddEncashDialog extends ConsumerStatefulWidget {
  const _AddEncashDialog({required this.lwdYear});
  final int lwdYear;

  @override
  ConsumerState<_AddEncashDialog> createState() => _AddEncashDialogState();
}

class _AddEncashDialogState extends ConsumerState<_AddEncashDialog> {
  final _form = GlobalKey<FormState>();
  final _days = TextEditingController();
  LeaveTypeRef? _type;
  late int _year = widget.lwdYear;

  @override
  void dispose() {
    _days.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final types = ref.watch(encashableLeaveTypesProvider);
    return AlertDialog(
      title: const Text('Add leave encashment'),
      content: Form(
        key: _form,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            types.when(
              loading: () => const Padding(padding: EdgeInsets.all(12), child: CircularProgressIndicator()),
              error: (e, _) => Text(payrollMsg(e)),
              data: (list) => list.isEmpty
                  ? const Text('No leave type can be encashed. Turn on "Encashable" in a leave type first.')
                  : DropdownButtonFormField<LeaveTypeRef>(
                      key: const ValueKey('enc-type'),
                      initialValue: _type,
                      isExpanded: true,
                      decoration: const InputDecoration(labelText: 'Leave type', border: OutlineInputBorder()),
                      items: [for (final t in list) DropdownMenuItem(value: t, child: Text(t.name))],
                      onChanged: (v) => setState(() => _type = v),
                      validator: (v) => v == null ? 'Choose a leave type' : null,
                    ),
            ),
            const SizedBox(height: 12),
            DropdownButtonFormField<int>(
              key: const ValueKey('enc-year'),
              initialValue: _year,
              decoration: const InputDecoration(labelText: 'Year', border: OutlineInputBorder()),
              items: [for (final y in [widget.lwdYear, widget.lwdYear - 1]) DropdownMenuItem(value: y, child: Text('$y'))],
              onChanged: (v) => setState(() => _year = v ?? _year),
            ),
            const SizedBox(height: 12),
            TextFormField(
              key: const ValueKey('enc-new-days'),
              controller: _days,
              keyboardType: const TextInputType.numberWithOptions(decimal: true),
              decoration: const InputDecoration(labelText: 'Days', border: OutlineInputBorder()),
              validator: validateEncashDays,
            ),
          ],
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(
          key: const ValueKey('enc-confirm'),
          onPressed: () {
            if (!(_form.currentState?.validate() ?? false)) return;
            Navigator.pop(context, _EncLine(type: _type!, year: _year, days: parseEncashDays(_days.text)!));
          },
          child: const Text('Add'),
        ),
      ],
    );
  }
}
