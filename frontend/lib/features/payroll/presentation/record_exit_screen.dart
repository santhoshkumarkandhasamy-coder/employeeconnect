import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import '../data/payroll_repository.dart';
import '../domain/phase4b_models.dart';
import 'fnf_detail_screen.dart';
import 'fnf_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// "Record exit": creates a draft final settlement (contract 18.8 POST). It
/// also sets the employee's last working day. Needs `payroll.manage`.
class RecordExitScreen extends ConsumerStatefulWidget {
  const RecordExitScreen({required this.employee, super.key, this.initial});
  final EmployeeRef employee;

  /// Prefilled values (tests).
  final ExitValues? initial;

  @override
  ConsumerState<RecordExitScreen> createState() => _RecordExitScreenState();
}

class _RecordExitScreenState extends ConsumerState<RecordExitScreen> {
  late ExitValues _values = widget.initial ??
      const ExitValues(lastWorkingDay: null, exitReason: 'resignation', settlementPeriod: null);
  final _remarks = TextEditingController();
  final _form = GlobalKey<FormState>();
  Map<String, String> _errors = const {};
  bool _saving = false;

  @override
  void dispose() {
    _remarks.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    if (_saving) return;
    final today = ref.read(payrollClockProvider)();
    final errors = validateExit(_values, today);
    final formOk = _form.currentState?.validate() ?? false;
    setState(() => _errors = errors);
    if (errors.isNotEmpty || !formOk) return;
    setState(() => _saving = true);
    final repo = ref.read(payrollRepositoryProvider);
    final nav = Navigator.of(context);
    try {
      final f = await repo.createFnf(widget.employee.id, fnfCreateBody(_values, remarks: _remarks.text));
      invalidateFnfState(ref);
      ref.invalidate(payrollDetailsProvider);
      await nav.pushReplacement<void, void>(
        MaterialPageRoute<void>(builder: (_) => FnfDetailScreen(fnfId: f.id)),
      );
    } on ApiException catch (e) {
      if (mounted) payrollSnack(context, fnfErrorMessage(e));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final today = ref.read(payrollClockProvider)();
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: Text('Record exit - ${widget.employee.display}')),
        body: Form(
          key: _form,
          child: ListView(
            padding: const EdgeInsets.all(16),
            children: [
              Text(
                'This starts a full and final settlement as a draft. It sets the last working day for '
                'payroll. Another person approves it before anything is paid.',
                style: Theme.of(context).textTheme.bodySmall,
              ),
              const SizedBox(height: 16),
              ExitFields(
                initial: _values,
                today: today,
                errors: _errors,
                onChanged: (v) => _values = v,
              ),
              const SizedBox(height: 12),
              TextFormField(
                key: const ValueKey('exit-remarks'),
                controller: _remarks,
                maxLength: 200,
                maxLines: 2,
                minLines: 1,
                decoration: const InputDecoration(labelText: 'Remarks (optional)', border: OutlineInputBorder()),
                validator: (v) {
                  final t = (v ?? '').trim();
                  if (t.isEmpty) return null;
                  return t.length < 3 ? 'At least 3 characters' : null;
                },
              ),
              const SizedBox(height: 16),
              FilledButton(
                key: const ValueKey('exit-save'),
                style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
                onPressed: _saving ? null : _save,
                child: _saving
                    ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                    : const Text('Create settlement'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
