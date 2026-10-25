import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../admin/presentation/admin_widgets.dart' show confirmAction;
import '../data/payroll_repository.dart';
import '../domain/gross_split.dart';
import '../domain/payroll_logic.dart';
import '../domain/payroll_models.dart';
import '../domain/payroll_money.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

class _AllowanceRow {
  _AllowanceRow({String code = '', String name = '', String amount = '', this.prorate = true, this.esiWage = true})
      : code = TextEditingController(text: code),
        name = TextEditingController(text: name),
        amount = TextEditingController(text: amount);
  final TextEditingController code;
  final TextEditingController name;
  final TextEditingController amount;
  bool prorate;
  bool esiWage;

  void dispose() {
    code.dispose();
    name.dispose();
    amount.dispose();
  }
}

class _DeductionRow {
  _DeductionRow({String code = '', String name = '', String amount = ''})
      : code = TextEditingController(text: code),
        name = TextEditingController(text: name),
        amount = TextEditingController(text: amount);
  final TextEditingController code;
  final TextEditingController name;
  final TextEditingController amount;

  void dispose() {
    code.dispose();
    name.dispose();
    amount.dispose();
  }
}

/// Add, copy or edit an effective-dated salary structure. A structure that a
/// processed or published payslip already uses is read-only (add a new one).
class StructureFormScreen extends ConsumerStatefulWidget {
  const StructureFormScreen({required this.userId, super.key, this.existing, this.copyFrom});
  final String userId;

  /// Edit this structure (PUT = full replace).
  final SalaryStructure? existing;

  /// Start a new structure with the values of this one.
  final SalaryStructure? copyFrom;

  @override
  ConsumerState<StructureFormScreen> createState() => _StructureFormScreenState();
}

class _StructureFormScreenState extends ConsumerState<StructureFormScreen> {
  final _form = GlobalKey<FormState>();
  final _basic = TextEditingController();
  final _hra = TextEditingController();
  final _tds = TextEditingController();
  final _grossTarget = TextEditingController();
  final List<_AllowanceRow> _allowances = [];
  final List<_DeductionRow> _deductions = [];
  late int _year;
  late int _month;
  bool _pf = true;
  bool _pfFull = false;
  bool _esi = true;
  bool _pt = true;
  bool _saving = false;
  String? _listError;

  SalaryStructure? get _source => widget.existing ?? widget.copyFrom;
  bool get _readOnly => widget.existing?.inUse ?? false;

  @override
  void initState() {
    super.initState();
    final now = ref.read(payrollClockProvider)();
    final eff = parseYmd(widget.existing?.effectiveFrom);
    _year = eff?.year ?? now.year;
    _month = eff?.month ?? now.month;
    final s = _source;
    if (s != null) {
      _basic.text = s.basic;
      _hra.text = s.hra;
      _tds.text = s.tdsMonthly;
      _pf = s.pfApplicable;
      _pfFull = s.pfOnFullBasic;
      _esi = s.esiApplicable;
      _pt = s.ptApplicable;
      for (final a in s.allowances) {
        _allowances.add(_AllowanceRow(
          code: a.code,
          name: a.name,
          amount: a.amount,
          prorate: a.prorate,
          esiWage: a.esiWage,
        ),);
      }
      for (final d in s.fixedDeductions) {
        _deductions.add(_DeductionRow(code: d.code, name: d.name, amount: d.amount));
      }
    } else {
      _hra.text = '0.00';
      _tds.text = '0.00';
    }
  }

  @override
  void dispose() {
    for (final c in [_basic, _hra, _tds, _grossTarget]) {
      c.dispose();
    }
    for (final r in _allowances) {
      r.dispose();
    }
    for (final r in _deductions) {
      r.dispose();
    }
    super.dispose();
  }

  StructureInput _input() => StructureInput(
        effectiveFrom: structureEffectiveFrom(_year, _month),
        basic: normalizeMoney(_basic.text) ?? '0.00',
        hra: normalizeMoney(_hra.text) ?? '0.00',
        allowances: [
          for (final r in _allowances)
            AllowanceLine(
              code: r.code.text.trim().toUpperCase(),
              name: r.name.text.trim(),
              amount: normalizeMoney(r.amount.text) ?? '0.00',
              prorate: r.prorate,
              esiWage: r.esiWage,
            ),
        ],
        fixedDeductions: [
          for (final r in _deductions)
            FixedDeductionLine(
              code: r.code.text.trim().toUpperCase(),
              name: r.name.text.trim(),
              amount: normalizeMoney(r.amount.text) ?? '0.00',
            ),
        ],
        tdsMonthly: normalizeMoney(_tds.text) ?? '0.00',
        pfApplicable: _pf,
        pfOnFullBasic: _pf && _pfFull,
        esiApplicable: _esi,
        ptApplicable: _pt,
      );

  void _splitFromGross() {
    final paise = parseMoneyPaise(_grossTarget.text);
    if (paise == null || paise < 100) {
      payrollSnack(context, 'Enter the total monthly gross first');
      return;
    }
    final split = splitGross((paise + 50) ~/ 100);
    setState(() {
      _basic.text = paiseToMoney(split.basic * 100);
      _hra.text = paiseToMoney(split.hra * 100);
      _listError = null;
      _AllowanceRow? row;
      for (final r in _allowances) {
        if (r.code.text.trim().toUpperCase() == splitAllowanceCode) row = r;
      }
      if (row == null) {
        if (_allowances.length >= 15) {
          payrollSnack(context, 'Too many allowances to add $splitAllowanceCode');
          return;
        }
        row = _AllowanceRow(code: splitAllowanceCode, name: splitAllowanceName);
        _allowances.add(row);
      }
      row.amount.text = paiseToMoney(split.allowance * 100);
    });
  }

  Future<void> _save() async {
    if (_saving) return;
    setState(() => _listError = null);
    if (!(_form.currentState?.validate() ?? false)) return;
    final input = _input();
    final listError = validateStructureLists(input.allowances, input.fixedDeductions);
    if (listError != null) {
      setState(() => _listError = listError);
      return;
    }
    setState(() => _saving = true);
    final repo = ref.read(payrollRepositoryProvider);
    final nav = Navigator.of(context);
    try {
      if (widget.existing != null) {
        await repo.updateStructure(widget.existing!.id, input);
      } else {
        await repo.createStructure(widget.userId, input);
      }
      nav.pop(true);
    } on ApiException catch (e) {
      if (mounted) payrollSnack(context, payrollErrorMessage(e));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _delete() async {
    final ok = await confirmAction(
      context,
      title: 'Delete this structure?',
      message: 'Only possible while no processed or published payslip uses it.',
      confirmLabel: 'Delete',
    );
    if (!ok || !mounted) return;
    final repo = ref.read(payrollRepositoryProvider);
    final nav = Navigator.of(context);
    try {
      await repo.deleteStructure(widget.existing!.id);
      nav.pop(true);
    } on ApiException catch (e) {
      if (mounted) payrollSnack(context, payrollErrorMessage(e));
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final now = ref.read(payrollClockProvider)();
    final years = [for (var y = now.year - 5; y <= now.year + 1; y++) y];
    if (!years.contains(_year)) years.add(_year);
    final gross = _input().monthlyGross;
    final ro = _readOnly;
    final title = widget.existing != null ? 'Salary structure' : 'New salary structure';

    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(
          title: Text(title),
          actions: [
            if (widget.existing != null && !ro)
              IconButton(
                tooltip: 'Delete structure',
                icon: const Icon(Icons.delete_outline),
                onPressed: _delete,
              ),
          ],
        ),
        body: Form(
          key: _form,
          child: ListView(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 48),
            children: [
              if (ro)
                Card(
                  color: theme.colorScheme.tertiaryContainer,
                  child: Padding(
                    padding: const EdgeInsets.all(12),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text(
                          'A processed or published payslip uses this structure, so it cannot be '
                          'changed. Add a new structure with a later start month instead.',
                        ),
                        TextButton(
                          onPressed: () => Navigator.of(context).pushReplacement(
                            MaterialPageRoute<bool>(
                              builder: (_) => StructureFormScreen(
                                userId: widget.userId,
                                copyFrom: widget.existing,
                              ),
                            ),
                          ),
                          child: const Text('Copy as a new structure'),
                        ),
                      ],
                    ),
                  ),
                ),
              Text('Effective from', style: theme.textTheme.titleSmall),
              const SizedBox(height: 8),
              Row(
                children: [
                  Expanded(
                    flex: 3,
                    child: DropdownButtonFormField<int>(
                      initialValue: _month,
                      isExpanded: true,
                      decoration: const InputDecoration(labelText: 'Month', border: OutlineInputBorder()),
                      items: [
                        for (var m = 1; m <= 12; m++)
                          DropdownMenuItem(value: m, child: Text(monthNames[m - 1])),
                      ],
                      onChanged: ro || widget.existing != null ? null : (v) => setState(() => _month = v ?? _month),
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    flex: 2,
                    child: DropdownButtonFormField<int>(
                      initialValue: _year,
                      isExpanded: true,
                      decoration: const InputDecoration(labelText: 'Year', border: OutlineInputBorder()),
                      items: [for (final y in years) DropdownMenuItem(value: y, child: Text('$y'))],
                      onChanged: ro || widget.existing != null ? null : (v) => setState(() => _year = v ?? _year),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 4),
              Text(
                'Always the 1st of the month. A mid-month change is paid as arrears.',
                style: theme.textTheme.bodySmall,
              ),
              const SizedBox(height: 16),
              if (!ro) ...[
                Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Expanded(
                      child: TextFormField(
                        key: const ValueKey('gross-target'),
                        controller: _grossTarget,
                        keyboardType: const TextInputType.numberWithOptions(decimal: true),
                        inputFormatters: [FilteringTextInputFormatter.allow(RegExp(r'[0-9.]'))],
                        decoration: const InputDecoration(
                          labelText: 'Total monthly gross',
                          prefixText: '₹ ',
                          border: OutlineInputBorder(),
                          helperText: 'Basic $grossBasicPercent% of gross, HRA $basicHraPercent% of basic, '
                              'rest in $splitAllowanceCode',
                          helperMaxLines: 2,
                        ),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Padding(
                      padding: const EdgeInsets.only(top: 6),
                      child: FilledButton.tonal(
                        key: const ValueKey('split-from-gross'),
                        onPressed: _splitFromGross,
                        child: const Text('Split from gross'),
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 16),
              ],
              MoneyField(
                controller: _basic,
                label: 'Basic (monthly)',
                positive: true,
                enabled: !ro,
                onChanged: () => setState(() {}),
              ),
              const SizedBox(height: 12),
              MoneyField(
                controller: _hra,
                label: 'HRA (monthly)',
                enabled: !ro,
                onChanged: () => setState(() {}),
              ),
              const SizedBox(height: 16),
              _sectionHeader('Allowances', onAdd: ro || _allowances.length >= 15
                  ? null
                  : () => setState(() => _allowances.add(_AllowanceRow())),),
              for (var i = 0; i < _allowances.length; i++) _allowanceCard(i, ro),
              const SizedBox(height: 8),
              _sectionHeader('Fixed deductions', onAdd: ro || _deductions.length >= 10
                  ? null
                  : () => setState(() => _deductions.add(_DeductionRow())),),
              for (var i = 0; i < _deductions.length; i++) _deductionCard(i, ro),
              const SizedBox(height: 8),
              MoneyField(
                controller: _tds,
                label: 'Default TDS (monthly)',
                enabled: !ro,
                helper: 'Copied into each payslip, where it can be edited',
              ),
              const SizedBox(height: 8),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('Provident fund applies'),
                value: _pf,
                onChanged: ro ? null : (v) => setState(() => _pf = v),
              ),
              if (_pf)
                SwitchListTile(
                  contentPadding: EdgeInsets.zero,
                  title: const Text('PF on full basic'),
                  subtitle: const Text('Ignore the PF wage ceiling'),
                  value: _pfFull,
                  onChanged: ro ? null : (v) => setState(() => _pfFull = v),
                ),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('ESI applies'),
                subtitle: const Text('Still subject to the wage threshold'),
                value: _esi,
                onChanged: ro ? null : (v) => setState(() => _esi = v),
              ),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('Professional tax applies'),
                value: _pt,
                onChanged: ro ? null : (v) => setState(() => _pt = v),
              ),
              const Divider(),
              AmountRow(
                label: 'Monthly gross',
                value: formatRupees(gross),
                bold: true,
                caption: 'Basic + HRA + allowances',
              ),
              if (_listError != null)
                Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: Text(_listError!, style: TextStyle(color: theme.colorScheme.error)),
                ),
              const SizedBox(height: 16),
              if (!ro)
                FilledButton(
                  key: const ValueKey('save-structure'),
                  style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
                  onPressed: _saving ? null : _save,
                  child: _saving
                      ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                      : const Text('Save structure'),
                ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _sectionHeader(String title, {VoidCallback? onAdd}) => Row(
        children: [
          Expanded(child: Text(title, style: Theme.of(context).textTheme.titleSmall)),
          TextButton.icon(
            onPressed: onAdd,
            icon: const Icon(Icons.add),
            label: const Text('Add'),
          ),
        ],
      );

  Widget _allowanceCard(int i, bool ro) {
    final r = _allowances[i];
    return Card(
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          children: [
            Row(
              children: [
                Expanded(
                  child: TextFormField(
                    controller: r.code,
                    enabled: !ro,
                    textCapitalization: TextCapitalization.characters,
                    decoration: const InputDecoration(labelText: 'Code', border: OutlineInputBorder()),
                    validator: validatePayrollCode,
                  ),
                ),
                if (!ro)
                  IconButton(
                    tooltip: 'Remove allowance',
                    icon: const Icon(Icons.delete_outline),
                    onPressed: () => setState(() => _allowances.removeAt(i).dispose()),
                  ),
              ],
            ),
            const SizedBox(height: 8),
            TextFormField(
              controller: r.name,
              enabled: !ro,
              maxLength: 40,
              decoration: const InputDecoration(labelText: 'Name', border: OutlineInputBorder()),
              validator: validatePayrollLabel,
            ),
            MoneyField(
              controller: r.amount,
              label: 'Amount (monthly)',
              enabled: !ro,
              onChanged: () => setState(() {}),
            ),
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              title: const Text('Prorate for loss of pay'),
              value: r.prorate,
              onChanged: ro ? null : (v) => setState(() => r.prorate = v),
            ),
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              title: const Text('Counts as ESI wage'),
              value: r.esiWage,
              onChanged: ro ? null : (v) => setState(() => r.esiWage = v),
            ),
          ],
        ),
      ),
    );
  }

  Widget _deductionCard(int i, bool ro) {
    final r = _deductions[i];
    return Card(
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          children: [
            Row(
              children: [
                Expanded(
                  child: TextFormField(
                    controller: r.code,
                    enabled: !ro,
                    textCapitalization: TextCapitalization.characters,
                    decoration: const InputDecoration(labelText: 'Code', border: OutlineInputBorder()),
                    validator: validatePayrollCode,
                  ),
                ),
                if (!ro)
                  IconButton(
                    tooltip: 'Remove deduction',
                    icon: const Icon(Icons.delete_outline),
                    onPressed: () => setState(() => _deductions.removeAt(i).dispose()),
                  ),
              ],
            ),
            const SizedBox(height: 8),
            TextFormField(
              controller: r.name,
              enabled: !ro,
              maxLength: 40,
              decoration: const InputDecoration(labelText: 'Name', border: OutlineInputBorder()),
              validator: validatePayrollLabel,
            ),
            MoneyField(controller: r.amount, label: 'Amount (monthly)', enabled: !ro),
          ],
        ),
      ),
    );
  }
}
