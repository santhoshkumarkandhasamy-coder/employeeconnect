import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import '../data/payroll_repository.dart';
import '../domain/payroll_logic.dart' show structureEffectiveFrom;
import '../domain/payroll_models.dart' show payrollErrorMessage;
import '../domain/phase4a_models.dart';
import 'increment_form_screen.dart';
import 'pay_items_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';
import 'structure_form_screen.dart';

/// CTC calculator (contract 18.2): from a gross or a CTC target to the salary
/// split, employer cost and a net estimate. Pure calculation: nothing is saved
/// and no employee data is used. TDS is not included in the net estimate.
class CtcCalculatorScreen extends ConsumerStatefulWidget {
  const CtcCalculatorScreen({super.key, this.canManage = false});

  /// Shows "Use as structure" (needs `payroll.manage`).
  final bool canManage;

  @override
  ConsumerState<CtcCalculatorScreen> createState() => _CtcCalculatorScreenState();
}

class _CtcCalculatorScreenState extends ConsumerState<CtcCalculatorScreen> {
  final _form = GlobalKey<FormState>();
  final _amount = TextEditingController();
  final _basicPct = TextEditingController(text: '50');
  final _hraPct = TextEditingController(text: '40');
  String _mode = 'from_gross';
  String _frequency = 'monthly';
  bool _pf = true;
  bool _pfFull = false;
  bool _esi = true;
  bool _pt = true;
  bool _customSplit = false;
  bool? _gratuity; // null = company setting
  bool? _employerShare;
  bool _busy = false;
  String? _error;
  CtcResult? _result;
  CtcInput? _sent;

  @override
  void dispose() {
    _amount.dispose();
    _basicPct.dispose();
    _hraPct.dispose();
    super.dispose();
  }

  String? _pctValidator(String? v, int min) {
    final n = int.tryParse((v ?? '').trim());
    if (n == null) return 'Enter a whole number';
    if (n < min || n > 100) return '$min to 100';
    return null;
  }

  Future<void> _calculate() async {
    if (_busy || !(_form.currentState?.validate() ?? false)) return;
    final input = CtcInput(
      mode: _mode,
      amount: _normalizeAmount(_amount.text),
      frequency: _frequency,
      pfApplicable: _pf,
      pfOnFullBasic: _pfFull,
      esiApplicable: _esi,
      ptApplicable: _pt,
      basicPct: _customSplit ? int.parse(_basicPct.text.trim()) : null,
      hraPctOfBasic: _customSplit ? int.parse(_hraPct.text.trim()) : null,
      includeGratuity: _gratuity,
      employerShareFromEmployee: _employerShare,
    );
    setState(() {
      _busy = true;
      _error = null;
    });
    final repo = ref.read(payrollRepositoryProvider);
    try {
      final r = await repo.ctcCalculate(input);
      if (mounted) {
        setState(() {
          _result = r;
          _sent = input;
        });
      }
    } on ApiException catch (e) {
      if (mounted) {
        setState(() {
          _error = payrollErrorMessage(e);
          _result = null;
        });
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  /// "40000" -> "40000.00" (the wire form).
  static String _normalizeAmount(String text) {
    final p = parseCtcPaise(text);
    if (p == null) return text.trim();
    return '${p ~/ 100}.${(p % 100).toString().padLeft(2, '0')}';
  }

  Future<void> _useAsStructure(CtcResult r, CtcInput input) async {
    final choice = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Use as structure'),
        content: const Text(
          'Choose an employee next. The split opens in the structure form for you to check. '
          'An employee who already has a structure needs an increment, which another person approves.',
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
          OutlinedButton(
            key: const ValueKey('use-structure'),
            onPressed: () => Navigator.pop(ctx, 'structure'),
            child: const Text('First structure'),
          ),
          FilledButton(
            key: const ValueKey('use-increment'),
            onPressed: () => Navigator.pop(ctx, 'increment'),
            child: const Text('Increment'),
          ),
        ],
      ),
    );
    if (choice == null || !mounted) return;
    final EmployeeRef? who = await showDialog<EmployeeRef>(
      context: context,
      builder: (_) => const EmployeePickerDialog(),
    );
    if (who == null || !mounted) return;
    final now = ref.read(payrollClockProvider)();
    final eff = structureEffectiveFrom(now.year, now.month);
    final structure = r.toStructureInput(input, eff);
    if (choice == 'structure') {
      await Navigator.of(context).push<bool>(
        MaterialPageRoute<bool>(
          builder: (_) => StructureFormScreen(
            userId: who.id,
            copyFrom: salaryStructureFromInput(who.id, structure),
          ),
        ),
      );
    } else {
      await Navigator.of(context).push<bool>(
        MaterialPageRoute<bool>(
          builder: (_) => IncrementFormScreen(
            userId: who.id,
            userName: who.display,
            initialStructure: structure,
          ),
        ),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final annual = _frequency == 'annual';
    final r = _result;
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('CTC calculator')),
        body: Form(
          key: _form,
          child: ListView(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 48),
            children: [
              SegmentedButton<String>(
                key: const ValueKey('ctc-mode'),
                segments: const [
                  ButtonSegment(value: 'from_gross', label: Text('From gross')),
                  ButtonSegment(value: 'from_ctc', label: Text('From CTC')),
                ],
                selected: {_mode},
                onSelectionChanged: (s) => setState(() {
                  _mode = s.first;
                  _result = null;
                }),
              ),
              const SizedBox(height: 12),
              SegmentedButton<String>(
                key: const ValueKey('ctc-frequency'),
                segments: const [
                  ButtonSegment(value: 'monthly', label: Text('Monthly')),
                  ButtonSegment(value: 'annual', label: Text('Annual')),
                ],
                selected: {_frequency},
                onSelectionChanged: (s) => setState(() {
                  _frequency = s.first;
                  _result = null;
                }),
              ),
              const SizedBox(height: 12),
              TextFormField(
                key: const ValueKey('ctc-amount'),
                controller: _amount,
                keyboardType: const TextInputType.numberWithOptions(decimal: true),
                inputFormatters: [FilteringTextInputFormatter.allow(RegExp(r'[0-9.,]'))],
                decoration: InputDecoration(
                  labelText: '${_mode == 'from_gross' ? 'Gross' : 'CTC'} (${annual ? 'per year' : 'per month'})',
                  prefixText: '₹ ',
                  border: const OutlineInputBorder(),
                ),
                validator: (v) => validateCtcAmount(v, annual: annual),
              ),
              const SizedBox(height: 8),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('Provident fund applies'),
                value: _pf,
                onChanged: (v) => setState(() => _pf = v),
              ),
              if (_pf)
                SwitchListTile(
                  contentPadding: EdgeInsets.zero,
                  title: const Text('PF on full basic'),
                  subtitle: const Text('Ignore the PF wage ceiling'),
                  value: _pfFull,
                  onChanged: (v) => setState(() => _pfFull = v),
                ),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('ESI applies'),
                subtitle: const Text('Still subject to the wage threshold'),
                value: _esi,
                onChanged: (v) => setState(() => _esi = v),
              ),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('Professional tax applies'),
                value: _pt,
                onChanged: (v) => setState(() => _pt = v),
              ),
              SwitchListTile(
                key: const ValueKey('ctc-custom-split'),
                contentPadding: EdgeInsets.zero,
                title: const Text('Use a different split'),
                subtitle: const Text('Otherwise the company rule from the payroll settings is used'),
                value: _customSplit,
                onChanged: (v) => setState(() => _customSplit = v),
              ),
              if (_customSplit)
                Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Expanded(
                      child: TextFormField(
                        key: const ValueKey('ctc-basic-pct'),
                        controller: _basicPct,
                        keyboardType: TextInputType.number,
                        inputFormatters: [FilteringTextInputFormatter.digitsOnly],
                        decoration: const InputDecoration(
                          labelText: 'Basic % of gross',
                          border: OutlineInputBorder(),
                        ),
                        validator: (v) => _pctValidator(v, 1),
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: TextFormField(
                        key: const ValueKey('ctc-hra-pct'),
                        controller: _hraPct,
                        keyboardType: TextInputType.number,
                        inputFormatters: [FilteringTextInputFormatter.digitsOnly],
                        decoration: const InputDecoration(
                          labelText: 'HRA % of basic',
                          border: OutlineInputBorder(),
                        ),
                        validator: (v) => _pctValidator(v, 0),
                      ),
                    ),
                  ],
                ),
              const SizedBox(height: 8),
              _triState(
                'ctc-gratuity',
                'Include gratuity accrual',
                _gratuity,
                (v) => setState(() => _gratuity = v),
              ),
              const SizedBox(height: 12),
              _triState(
                'ctc-share',
                'Employee pays the employer PF / ESI share',
                _employerShare,
                (v) => setState(() => _employerShare = v),
              ),
              const SizedBox(height: 16),
              FilledButton.icon(
                key: const ValueKey('ctc-calculate'),
                style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
                onPressed: _busy ? null : _calculate,
                icon: _busy
                    ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                    : const Icon(Icons.calculate_outlined),
                label: const Text('Calculate'),
              ),
              if (_error != null)
                Card(
                  key: const ValueKey('ctc-error'),
                  color: theme.colorScheme.errorContainer,
                  child: Padding(
                    padding: const EdgeInsets.all(12),
                    child: Text(_error!, style: TextStyle(color: theme.colorScheme.onErrorContainer)),
                  ),
                ),
              if (r != null) ..._resultCards(r),
            ],
          ),
        ),
      ),
    );
  }

  Widget _triState(String key, String label, bool? value, ValueChanged<bool?> onChanged) =>
      DropdownButtonFormField<bool?>(
        key: ValueKey(key),
        initialValue: value,
        isExpanded: true,
        decoration: InputDecoration(labelText: label, border: const OutlineInputBorder()),
        items: const [
          DropdownMenuItem<bool?>(child: Text('As in the payroll settings')),
          DropdownMenuItem<bool?>(value: true, child: Text('Yes')),
          DropdownMenuItem<bool?>(value: false, child: Text('No')),
        ],
        onChanged: onChanged,
      );

  List<Widget> _resultCards(CtcResult r) {
    final theme = Theme.of(context);
    final rules = r.rules;
    return [
      const SizedBox(height: 12),
      for (final f in r.flags)
        Padding(
          padding: const EdgeInsets.only(bottom: 4),
          child: Chip(
            avatar: const Icon(Icons.info_outline, size: 16),
            label: Text(ctcFlagLabel(f)),
            visualDensity: VisualDensity.compact,
          ),
        ),
      SectionCard(
        title: 'Monthly salary split',
        children: [
          AmountRow(label: 'Basic', value: formatBigRupees(r.basic)),
          AmountRow(label: 'HRA', value: formatBigRupees(r.hra)),
          for (final a in r.allowances) AmountRow(label: a.name.isEmpty ? a.code : a.name, value: formatBigRupees(a.amount)),
          const Divider(),
          AmountRow(label: 'Gross', value: formatBigRupees(r.monthlyGross), bold: true),
          if (r.mode == 'from_ctc' && r.unallocatedMonthly != '0.00')
            AmountRow(
              label: 'Not allocated (rounding to whole rupees)',
              value: formatBigRupees(r.unallocatedMonthly),
            ),
        ],
      ),
      SectionCard(
        title: 'Cost to company',
        children: [
          AmountRow(label: 'Gross', value: formatBigRupees(r.monthlyGross)),
          AmountRow(label: 'Employer PF', value: formatBigRupees(r.employerPf)),
          AmountRow(label: 'Employer ESI', value: formatBigRupees(r.employerEsi)),
          if (r.employerRecovered != '0.00')
            AmountRow(label: 'Recovered from the employee', value: '-${formatBigRupees(r.employerRecovered)}'),
          AmountRow(label: 'Gratuity accrual', value: formatBigRupees(r.gratuityAccrual)),
          const Divider(),
          AmountRow(label: 'CTC per month', value: formatBigRupees(r.monthlyCtc), bold: true, key: const ValueKey('ctc-monthly')),
          AmountRow(label: 'CTC per year', value: formatBigRupees(r.annualCtc), key: const ValueKey('ctc-annual')),
          AmountRow(label: 'Gross per year', value: formatBigRupees(r.annualGross)),
        ],
      ),
      SectionCard(
        title: 'Employee estimate',
        children: [
          AmountRow(label: 'PF', value: formatBigRupees(r.employeePf)),
          AmountRow(label: 'ESI', value: formatBigRupees(r.employeeEsi)),
          AmountRow(label: 'Professional tax (per year)', value: formatBigRupees(r.ptAnnualEstimate)),
          const Divider(),
          AmountRow(
            label: 'Net per month (estimate)',
            value: formatBigRupees(r.netMonthlyEstimate),
            bold: true,
            caption: 'Before income tax (TDS)',
            key: const ValueKey('ctc-net'),
          ),
        ],
      ),
      if (rules.isNotEmpty)
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 4),
          child: Text(
            'Rules used: PF ${rules['pf_employee_rate_pct']}% employee / ${rules['pf_employer_rate_pct']}% employer '
            'on wages up to ${rules['pf_wage_ceiling']}; ESI up to ${rules['esi_wage_threshold']} '
            '(${rules['esi_employee_rate_pct']}% / ${rules['esi_employer_rate_pct']}%); '
            'PT ${rules['pt_frequency']}; gratuity on ${rules['gratuity_wage_basis']}.',
            style: theme.textTheme.bodySmall,
          ),
        ),
      if (widget.canManage && _sent != null)
        OutlinedButton.icon(
          key: const ValueKey('ctc-use'),
          onPressed: () => _useAsStructure(r, _sent!),
          icon: const Icon(Icons.assignment_outlined),
          label: const Text('Use as structure'),
        ),
    ];
  }
}
