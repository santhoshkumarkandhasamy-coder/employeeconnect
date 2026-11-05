import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../admin/presentation/admin_widgets.dart' show adminPermissionsProvider;
import '../domain/payroll_models.dart' show canManagePayroll, periodTitle;
import '../domain/phase5a_models.dart';
import 'payment_batch_screen.dart';
import 'payment_widgets.dart';
import 'payroll_providers.dart';
import '../data/payroll_repository.dart';

/// Payment batches of one run, with "New batch" (`payroll.manage`).
class PaymentBatchesScreen extends ConsumerWidget {
  const PaymentBatchesScreen({required this.runId, super.key});
  final String runId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final manage = canManagePayroll(ref.watch(adminPermissionsProvider));
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Payments')),
        floatingActionButton: manage
            ? FloatingActionButton.extended(
                key: const ValueKey('new-batch'),
                onPressed: () async {
                  await Navigator.of(context).push<void>(
                    MaterialPageRoute<void>(builder: (_) => CreateBatchScreen(runId: runId)),
                  );
                  ref.invalidate(paymentBatchesProvider);
                },
                icon: const Icon(Icons.add),
                label: const Text('New batch'),
              )
            : null,
        body: PaymentBatchesList(runId: runId),
      ),
    );
  }
}

/// Create a draft batch for a published run: debit account (the default one if
/// none is chosen) and value date. Opens the new batch afterwards.
class CreateBatchScreen extends ConsumerStatefulWidget {
  const CreateBatchScreen({required this.runId, super.key, this.period});
  final String runId;
  final String? period;

  @override
  ConsumerState<CreateBatchScreen> createState() => _CreateBatchScreenState();
}

class _CreateBatchScreenState extends ConsumerState<CreateBatchScreen> {
  String? _accountId;
  DateTime? _date;
  bool _busy = false;
  String? _dateError;
  String? _error;

  DateTime get _today {
    final n = ref.read(payrollClockProvider)();
    return DateTime(n.year, n.month, n.day);
  }

  @override
  void initState() {
    super.initState();
    _date = _today;
  }

  Future<void> _pick() async {
    final t = _today;
    final d = await showDatePicker(
      context: context,
      initialDate: _date ?? t,
      firstDate: t,
      lastDate: t.add(const Duration(days: 30)),
    );
    if (d != null) setState(() => _date = d);
  }

  Future<void> _create() async {
    if (_busy) return;
    final err = validateValueDate(_date, ref.read(payrollClockProvider)());
    setState(() {
      _dateError = err;
      _error = null;
    });
    if (err != null) return;
    setState(() => _busy = true);
    final repo = ref.read(payrollRepositoryProvider);
    final nav = Navigator.of(context);
    try {
      final b = await repo.createPaymentBatch(
        widget.runId,
        PaymentBatchCreate(valueDate: ymd(_date!), debitAccountId: _accountId),
      );
      invalidatePaymentState(ref);
      if (!mounted) return;
      await nav.pushReplacement<void, void>(
        MaterialPageRoute<void>(builder: (_) => PaymentBatchScreen(batchId: b.id)),
      );
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = phase5ErrorMessage(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final accounts = ref.watch(bankAccountsProvider);
    final theme = Theme.of(context);
    final active = [for (final a in accounts.valueOrNull ?? const <BankAccount>[]) if (a.active) a];
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(
          title: Text(widget.period == null ? 'New payment batch' : 'New batch: ${periodTitle(widget.period!)}'),
        ),
        body: ListView(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 48),
          children: [
            const Text(
              'The batch pays every published payslip of the run that has valid bank details and is not paid yet. '
              'You can leave lines out before you submit it.',
            ),
            const SizedBox(height: 16),
            if (accounts.hasError)
              Text(p5Msg(accounts.error!), style: TextStyle(color: theme.colorScheme.error))
            else
              DropdownButtonFormField<String?>(
                key: const ValueKey('batch-account'),
                initialValue: _accountId,
                isExpanded: true,
                decoration: const InputDecoration(labelText: 'Debit account', border: OutlineInputBorder()),
                items: [
                  const DropdownMenuItem<String?>(child: Text('Default account')),
                  for (final a in active)
                    DropdownMenuItem<String?>(
                      value: a.id,
                      child: Text('${a.label} (${a.account})', overflow: TextOverflow.ellipsis),
                    ),
                ],
                onChanged: _busy ? null : (v) => setState(() => _accountId = v),
              ),
            const SizedBox(height: 12),
            InkWell(
              key: const ValueKey('batch-value-date'),
              onTap: _busy ? null : _pick,
              child: InputDecorator(
                decoration: InputDecoration(
                  labelText: 'Value date',
                  helperText: 'Today up to 30 days ahead',
                  border: const OutlineInputBorder(),
                  errorText: _dateError,
                  suffixIcon: const Icon(Icons.calendar_today_outlined),
                ),
                child: Text(_date == null ? 'Tap to choose' : formatDayLong(_date!)),
              ),
            ),
            if (_error != null)
              Padding(
                padding: const EdgeInsets.only(top: 12),
                child: Semantics(
                  liveRegion: true,
                  child: Text(
                    _error!,
                    key: const ValueKey('batch-create-error'),
                    style: TextStyle(color: theme.colorScheme.error),
                  ),
                ),
              ),
            const SizedBox(height: 16),
            FilledButton(
              key: const ValueKey('batch-create'),
              style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
              onPressed: _busy ? null : _create,
              child: _busy
                  ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Text('Create draft batch'),
            ),
          ],
        ),
      ),
    );
  }
}
