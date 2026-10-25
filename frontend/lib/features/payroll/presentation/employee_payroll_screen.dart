import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import '../../admin/presentation/admin_widgets.dart' show adminPermissionsProvider, adminSelfIdProvider;
import '../data/payroll_repository.dart';
import '../domain/payroll_logic.dart';
import '../domain/payroll_models.dart';
import '../domain/payroll_money.dart';
import 'payroll_details_screen.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';
import 'structure_form_screen.dart';

/// One employee's payroll set-up: effective-dated salary structures and the
/// payroll details (bank, PAN, UAN, ESI). Sensitive identifiers are masked
/// until "Reveal" is used; the revealed copy lives only in this screen's state
/// and is dropped after a minute and on exit.
class EmployeePayrollScreen extends ConsumerStatefulWidget {
  const EmployeePayrollScreen({required this.employee, super.key});
  final EmployeeRef employee;

  @override
  ConsumerState<EmployeePayrollScreen> createState() => _EmployeePayrollScreenState();
}

class _EmployeePayrollScreenState extends ConsumerState<EmployeePayrollScreen> {
  PayrollDetails? _revealed;
  Timer? _hide;
  bool _revealing = false;

  String get _userId => widget.employee.id;

  @override
  void dispose() {
    _hide?.cancel();
    _revealed = null;
    super.dispose();
  }

  Future<void> _reveal() async {
    if (_revealing) return;
    setState(() => _revealing = true);
    final repo = ref.read(payrollRepositoryProvider);
    try {
      final d = await repo.details(_userId, reveal: true);
      if (!mounted) return;
      setState(() => _revealed = d);
      _hide?.cancel();
      _hide = Timer(const Duration(seconds: 60), () {
        if (mounted) setState(() => _revealed = null);
      });
    } on ApiException catch (e) {
      if (mounted) payrollSnack(context, payrollErrorMessage(e));
    } finally {
      if (mounted) setState(() => _revealing = false);
    }
  }

  Future<void> _editDetails(PayrollDetails current) async {
    final saved = await Navigator.of(context).push<bool>(
      MaterialPageRoute<bool>(
        builder: (_) => PayrollDetailsScreen(employee: widget.employee, initial: current),
      ),
    );
    if (saved == true) {
      _hide?.cancel();
      setState(() => _revealed = null);
      ref.invalidate(payrollDetailsProvider);
    }
  }

  Future<void> _openStructure({SalaryStructure? existing, SalaryStructure? copyFrom}) async {
    final saved = await Navigator.of(context).push<bool>(
      MaterialPageRoute<bool>(
        builder: (_) => StructureFormScreen(
          userId: _userId,
          existing: existing,
          copyFrom: copyFrom,
        ),
      ),
    );
    if (saved == true) ref.invalidate(payrollStructuresProvider(_userId));
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    final self = ref.watch(adminSelfIdProvider) == _userId;
    final canEdit = canManagePayroll(perms) && !self;
    final structures = ref.watch(payrollStructuresProvider(_userId));
    final details = ref.watch(payrollDetailsProvider((_userId, false)));
    final theme = Theme.of(context);

    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: Text(widget.employee.display)),
        body: ListView(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 48),
          children: [
            if (self)
              const Padding(
                padding: EdgeInsets.only(bottom: 8),
                child: Text('This is you. You cannot change your own payroll data.'),
              ),
            Row(
              children: [
                Expanded(child: Text('Salary structures', style: theme.textTheme.titleMedium)),
                if (canEdit)
                  TextButton.icon(
                    key: const ValueKey('add-structure'),
                    onPressed: () => _openStructure(
                      copyFrom: structures.valueOrNull?.isNotEmpty ?? false
                          ? structures.valueOrNull!.first
                          : null,
                    ),
                    icon: const Icon(Icons.add),
                    label: const Text('Add'),
                  ),
              ],
            ),
            structures.when(
              skipLoadingOnReload: true,
              loading: () => const Padding(
                padding: EdgeInsets.all(24),
                child: LoadingView(label: 'Loading structures'),
              ),
              error: (e, _) => ErrorView(
                message: payrollMsg(e),
                onRetry: () => ref.invalidate(payrollStructuresProvider(_userId)),
              ),
              data: (items) => items.isEmpty
                  ? const EmptyView(
                      icon: Icons.account_balance_wallet_outlined,
                      message: 'No salary structure yet. Payroll cannot be processed without one.',
                    )
                  : Column(
                      children: [
                        for (final s in items)
                          Card(
                            margin: const EdgeInsets.symmetric(vertical: 4),
                            child: ListTile(
                              title: Text('From ${_monthLabel(s.effectiveFrom)}'),
                              subtitle: Text(
                                'Gross ${formatRupees(s.monthlyGross)}'
                                '${s.effectiveTo == null ? '  -  current' : '  -  until ${s.effectiveTo}'}',
                              ),
                              trailing: Row(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  if (s.inUse)
                                    const Tooltip(
                                      message: 'Used by a processed or published payslip',
                                      child: Icon(Icons.lock_outline),
                                    ),
                                  const Icon(Icons.chevron_right),
                                ],
                              ),
                              onTap: () => _openStructure(existing: s),
                            ),
                          ),
                      ],
                    ),
            ),
            const SizedBox(height: 16),
            Row(
              children: [
                Expanded(child: Text('Payroll details', style: theme.textTheme.titleMedium)),
                if (_revealed == null)
                  TextButton.icon(
                    key: const ValueKey('reveal-details'),
                    onPressed: _revealing ? null : _reveal,
                    icon: const Icon(Icons.visibility_outlined),
                    label: const Text('Reveal'),
                  )
                else
                  TextButton.icon(
                    onPressed: () => setState(() => _revealed = null),
                    icon: const Icon(Icons.visibility_off_outlined),
                    label: const Text('Hide'),
                  ),
              ],
            ),
            details.when(
              skipLoadingOnReload: true,
              loading: () => const Padding(
                padding: EdgeInsets.all(24),
                child: LoadingView(label: 'Loading details'),
              ),
              error: (e, _) => ErrorView(
                message: payrollMsg(e),
                onRetry: () => ref.invalidate(payrollDetailsProvider((_userId, false))),
              ),
              data: (masked) {
                final d = _revealed ?? masked;
                return Card(
                  child: Padding(
                    padding: const EdgeInsets.all(14),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        _row('Designation', d.designation),
                        _row('Payment mode', paymentModeLabel(d.paymentMode)),
                        _row('In payroll', d.includeInPayroll ? 'Yes' : 'No'),
                        _row('Bank', d.bankName),
                        _row('Account', d.bankAccount),
                        _row('IFSC', d.bankIfsc),
                        _row('PAN', d.pan),
                        _row('UAN', d.uan),
                        _row('ESI number', d.esiIpNumber),
                        if (canEdit)
                          Align(
                            alignment: Alignment.centerRight,
                            child: FilledButton.tonalIcon(
                              key: const ValueKey('edit-details'),
                              onPressed: () => _editDetails(d),
                              icon: const Icon(Icons.edit_outlined),
                              label: const Text('Edit'),
                            ),
                          ),
                      ],
                    ),
                  ),
                );
              },
            ),
          ],
        ),
      ),
    );
  }

  static String _monthLabel(String ymdText) {
    final d = parseYmd(ymdText);
    return d == null ? ymdText : '${monthNames[d.month - 1]} ${d.year}';
  }

  Widget _row(String k, String? v) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 3),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(width: 110, child: Text(k, style: Theme.of(context).textTheme.bodySmall)),
            Expanded(child: Text(v == null || v.isEmpty ? '-' : v)),
          ],
        ),
      );
}
