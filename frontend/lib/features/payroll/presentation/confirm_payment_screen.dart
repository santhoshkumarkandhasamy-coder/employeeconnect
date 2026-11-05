import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../admin/presentation/admin_widgets.dart' show runWithReauth;
import '../data/payroll_repository.dart';
import '../domain/payroll_money.dart';
import '../domain/phase5a_models.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// Confirm what the bank did with an exported batch (contract 19.5): the
/// payment date and either "all lines paid" or a paid / failed choice per
/// line, each with an optional bank reference (UTR). Needs the password.
/// Pops `true` when the server accepted it.
class ConfirmPaymentScreen extends ConsumerStatefulWidget {
  const ConfirmPaymentScreen({required this.batch, super.key});
  final PaymentBatch batch;

  @override
  ConsumerState<ConfirmPaymentScreen> createState() => _ConfirmPaymentScreenState();
}

class _ConfirmPaymentScreenState extends ConsumerState<ConfirmPaymentScreen> {
  final _form = GlobalKey<FormState>();
  final _batchRef = TextEditingController();
  final Map<String, TextEditingController> _refs = {};
  final Map<String, String?> _choice = {}; // line id -> paid | failed | null
  DateTime? _paidOn;
  bool _allPaid = true;
  bool _busy = false;
  String? _dateError;
  String? _error;

  List<PaymentLine> get _exported => [for (final l in widget.batch.lines) if (l.isExported) l];

  @override
  void initState() {
    super.initState();
    final now = ref.read(payrollClockProvider)();
    final today = DateTime(now.year, now.month, now.day);
    final ex = widget.batch.exportedAt;
    final exDay = ex == null ? null : DateTime(ex.year, ex.month, ex.day);
    _paidOn = exDay != null && exDay.isAfter(today) ? exDay : today;
    for (final l in _exported) {
      _refs[l.id] = TextEditingController();
    }
  }

  @override
  void dispose() {
    _batchRef.dispose();
    for (final c in _refs.values) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> _pickDate() async {
    final now = ref.read(payrollClockProvider)();
    final today = DateTime(now.year, now.month, now.day);
    final ex = widget.batch.exportedAt;
    final first = ex == null ? DateTime(today.year - 1, today.month, today.day) : DateTime(ex.year, ex.month, ex.day);
    final d = await showDatePicker(
      context: context,
      initialDate: _paidOn ?? today,
      firstDate: first.isAfter(today) ? today : first,
      lastDate: today,
    );
    if (d != null) setState(() => _paidOn = d);
  }

  Future<void> _submit() async {
    if (_busy) return;
    setState(() {
      _error = null;
      _dateError = null;
    });
    final now = ref.read(payrollClockProvider)();
    final dateErr = validatePaidOn(_paidOn, now, exportedAt: widget.batch.exportedAt);
    final formOk = _form.currentState?.validate() ?? false;
    if (dateErr != null) setState(() => _dateError = dateErr);
    if (dateErr != null || !formOk) return;
    List<ConfirmLine>? lines;
    if (!_allPaid) {
      lines = [
        for (final l in _exported)
          if (_choice[l.id] != null)
            ConfirmLine(lineId: l.id, status: _choice[l.id]!, reference: _refs[l.id]!.text),
      ];
      if (lines.isEmpty) {
        setState(() => _error = 'Mark at least one line as paid or failed.');
        return;
      }
    }
    final body = PaymentConfirm(
      paidOn: ymd(_paidOn!),
      lines: lines,
      allPaid: _allPaid,
      reference: _allPaid ? _batchRef.text : null,
    );
    setState(() => _busy = true);
    final repo = ref.read(payrollRepositoryProvider);
    try {
      final done = await runWithReauth(
        context,
        reason: 'Confirming payments needs your password.',
        action: (pw) async {
          await repo.confirmPayment(widget.batch.id, body, currentPassword: pw);
        },
      );
      if (!done || !mounted) return;
      payrollSnack(context, 'Payments recorded.');
      Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = phase5ErrorMessage(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final lines = _exported;
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: Text('Confirm ${widget.batch.reference}')),
        body: Form(
          key: _form,
          child: ListView(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 48),
            children: [
              InkWell(
                key: const ValueKey('confirm-paid-on'),
                onTap: _busy ? null : _pickDate,
                child: InputDecorator(
                  decoration: InputDecoration(
                    labelText: 'Paid on',
                    border: const OutlineInputBorder(),
                    errorText: _dateError,
                    suffixIcon: const Icon(Icons.calendar_today_outlined),
                  ),
                  child: Text(_paidOn == null ? 'Tap to choose' : formatDayLong(_paidOn!)),
                ),
              ),
              const SizedBox(height: 12),
              SegmentedButton<bool>(
                key: const ValueKey('confirm-mode'),
                segments: const [
                  ButtonSegment(value: true, label: Text('All paid')),
                  ButtonSegment(value: false, label: Text('Line by line')),
                ],
                selected: {_allPaid},
                onSelectionChanged: _busy ? null : (s) => setState(() => _allPaid = s.first),
              ),
              const SizedBox(height: 12),
              if (_allPaid) ...[
                Text(
                  'Every line in the file (${lines.length}) is marked as paid.',
                  style: theme.textTheme.bodyMedium,
                ),
                const SizedBox(height: 8),
                TextFormField(
                  key: const ValueKey('confirm-batch-ref'),
                  controller: _batchRef,
                  enabled: !_busy,
                  maxLength: 40,
                  decoration: const InputDecoration(
                    labelText: 'Bank reference for the batch (optional)',
                    border: OutlineInputBorder(),
                  ),
                  validator: validateBankReference,
                ),
              ] else ...[
                Text(
                  'Choose what the bank did for each line. Lines you leave unmarked stay waiting.',
                  style: theme.textTheme.bodyMedium,
                ),
                for (final l in lines)
                  Card(
                    key: ValueKey('confirm-line-${l.id}'),
                    margin: const EdgeInsets.symmetric(vertical: 4),
                    child: Padding(
                      padding: const EdgeInsets.all(12),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text('${l.seq}. ${l.user.display}', style: theme.textTheme.titleSmall),
                          Text(formatRupees(l.amount)),
                          const SizedBox(height: 6),
                          SegmentedButton<String?>(
                            emptySelectionAllowed: true,
                            segments: const [
                              ButtonSegment(value: 'paid', label: Text('Paid')),
                              ButtonSegment(value: 'failed', label: Text('Failed')),
                            ],
                            selected: {if (_choice[l.id] != null) _choice[l.id]},
                            onSelectionChanged: _busy
                                ? null
                                : (s) => setState(() => _choice[l.id] = s.isEmpty ? null : s.first),
                          ),
                          const SizedBox(height: 8),
                          TextFormField(
                            key: ValueKey('confirm-ref-${l.id}'),
                            controller: _refs[l.id],
                            enabled: !_busy,
                            maxLength: 40,
                            decoration: const InputDecoration(
                              labelText: 'Bank reference (optional)',
                              border: OutlineInputBorder(),
                            ),
                            validator: validateBankReference,
                          ),
                        ],
                      ),
                    ),
                  ),
              ],
              if (_error != null)
                Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: Semantics(
                    liveRegion: true,
                    child: Text(_error!, key: const ValueKey('confirm-error'), style: TextStyle(color: theme.colorScheme.error)),
                  ),
                ),
              const SizedBox(height: 16),
              FilledButton(
                key: const ValueKey('confirm-submit'),
                style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
                onPressed: _busy ? null : _submit,
                child: _busy
                    ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                    : const Text('Record payments'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
