import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import '../../admin/presentation/admin_widgets.dart' show confirmAction, runWithReauth;
import '../data/payroll_repository.dart';
import '../domain/payroll_logic.dart';
import '../domain/payroll_models.dart';
import 'payroll_widgets.dart';

/// Edit the payroll details of one employee. Needs `payroll.manage` and asks
/// for the caller's password on save (the server requires it for every change).
///
/// Masked values (account, PAN, UAN, ESI) are never copied into the fields:
/// when the details were not revealed, a blank field means "keep" and only
/// typed values are sent. Reveal first to see, change or clear a value.
class PayrollDetailsScreen extends ConsumerStatefulWidget {
  const PayrollDetailsScreen({required this.employee, required this.initial, super.key});
  final EmployeeRef employee;
  final PayrollDetails initial;

  @override
  ConsumerState<PayrollDetailsScreen> createState() => _PayrollDetailsScreenState();
}

class _PayrollDetailsScreenState extends ConsumerState<PayrollDetailsScreen> {
  final _form = GlobalKey<FormState>();
  late final _designation = TextEditingController(text: widget.initial.designation ?? '');
  late final _bankName = TextEditingController(text: widget.initial.bankName ?? '');
  late final _account = TextEditingController(text: _prefill(widget.initial.bankAccount));
  late final _ifsc = TextEditingController(text: _prefill(widget.initial.bankIfsc));
  late final _pan = TextEditingController(text: _prefill(widget.initial.pan));
  late final _uan = TextEditingController(text: _prefill(widget.initial.uan));
  late final _esi = TextEditingController(text: _prefill(widget.initial.esiIpNumber));
  late String _mode = widget.initial.paymentMode;
  late bool _include = widget.initial.includeInPayroll;
  bool _saving = false;

  String _prefill(String? v) => widget.initial.revealed ? (v ?? '') : '';

  @override
  void dispose() {
    for (final c in [_designation, _bankName, _account, _ifsc, _pan, _uan, _esi]) {
      c.dispose();
    }
    super.dispose();
  }

  String? _hint(String? masked) => widget.initial.revealed || masked == null
      ? null
      : 'Now $masked. Leave blank to keep.';

  Future<void> _save() async {
    if (_saving || !(_form.currentState?.validate() ?? false)) return;
    final patch = buildDetailsPatch(
      original: widget.initial,
      designation: _designation.text,
      paymentMode: _mode,
      include: _include,
      bankName: _bankName.text,
      bankAccount: _account.text,
      bankIfsc: _ifsc.text,
      pan: _pan.text,
      uan: _uan.text,
      esiIp: _esi.text,
    );
    if (patch.isEmpty) {
      payrollSnack(context, 'Nothing changed.');
      return;
    }
    final ok = await confirmAction(
      context,
      title: 'Save payroll details?',
      message: 'Changes to bank, PAN, UAN and ESI details are logged. '
          'You will be asked for your password.',
      confirmLabel: 'Continue',
    );
    if (!ok || !mounted) return;
    setState(() => _saving = true);
    final repo = ref.read(payrollRepositoryProvider);
    final nav = Navigator.of(context);
    try {
      final done = await runWithReauth(
        context,
        reason: 'Changing payroll details needs your password.',
        action: (pw) async {
          await repo.saveDetails(widget.employee.id, patch, currentPassword: pw);
        },
      );
      if (done) nav.pop(true);
    } on ApiException catch (e) {
      if (mounted) payrollSnack(context, payrollErrorMessage(e));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  InputDecoration _dec(String label, {String? helper}) => InputDecoration(
        labelText: label,
        helperText: helper,
        helperMaxLines: 2,
        border: const OutlineInputBorder(),
      );

  @override
  Widget build(BuildContext context) {
    final d = widget.initial;
    const gap = SizedBox(height: 14);
    final upper = [FilteringTextInputFormatter.deny(RegExp(r'\s'))];
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: Text('Payroll details - ${widget.employee.display}')),
        body: Form(
          key: _form,
          child: ListView(
            padding: const EdgeInsets.all(16),
            children: [
              TextFormField(
                controller: _designation,
                maxLength: 60,
                decoration: _dec('Designation'),
                validator: validateDesignation,
              ),
              DropdownButtonFormField<String>(
                initialValue: _mode,
                isExpanded: true,
                decoration: _dec('Payment mode'),
                items: [
                  for (final m in paymentModes)
                    DropdownMenuItem(value: m, child: Text(paymentModeLabel(m))),
                ],
                onChanged: (v) => setState(() => _mode = v ?? _mode),
              ),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('Include in payroll'),
                value: _include,
                onChanged: (v) => setState(() => _include = v),
              ),
              gap,
              TextFormField(controller: _bankName, maxLength: 60, decoration: _dec('Bank name')),
              TextFormField(
                controller: _account,
                enableSuggestions: false,
                autocorrect: false,
                enableIMEPersonalizedLearning: false,
                keyboardType: TextInputType.number,
                inputFormatters: [FilteringTextInputFormatter.allow(RegExp(r'[0-9 ]'))],
                decoration: _dec('Account number', helper: _hint(d.bankAccount)),
                validator: validateBankAccount,
              ),
              gap,
              TextFormField(
                controller: _ifsc,
                enableSuggestions: false,
                autocorrect: false,
                enableIMEPersonalizedLearning: false,
                textCapitalization: TextCapitalization.characters,
                inputFormatters: upper,
                decoration: _dec('IFSC', helper: _hint(d.bankIfsc)),
                validator: validateIfsc,
              ),
              gap,
              TextFormField(
                controller: _pan,
                enableSuggestions: false,
                autocorrect: false,
                enableIMEPersonalizedLearning: false,
                textCapitalization: TextCapitalization.characters,
                inputFormatters: upper,
                decoration: _dec('PAN', helper: _hint(d.pan)),
                validator: validatePan,
              ),
              gap,
              TextFormField(
                controller: _uan,
                enableSuggestions: false,
                autocorrect: false,
                enableIMEPersonalizedLearning: false,
                keyboardType: TextInputType.number,
                decoration: _dec('UAN (12 digits)', helper: _hint(d.uan)),
                validator: validateUan,
              ),
              gap,
              TextFormField(
                controller: _esi,
                enableSuggestions: false,
                autocorrect: false,
                enableIMEPersonalizedLearning: false,
                keyboardType: TextInputType.number,
                decoration: _dec('ESI number (10 digits)', helper: _hint(d.esiIpNumber)),
                validator: validateEsiIp,
              ),
              if (!d.revealed)
                const Padding(
                  padding: EdgeInsets.only(top: 12),
                  child: Text(
                    'To change or clear a hidden value, go back and use Reveal first.',
                  ),
                ),
              const SizedBox(height: 20),
              FilledButton(
                key: const ValueKey('save-details'),
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
}
