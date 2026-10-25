import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart'
    show adminPermissionsProvider, adminSelfIdProvider, confirmAction, runWithReauth;
import '../data/payroll_repository.dart';
import '../domain/payroll_logic.dart';
import '../domain/payroll_models.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';
import 'payslip_view.dart';

/// One payslip of a run (admin). A draft can be edited (LOP override with a
/// reason, TDS, PT override, adjustments, hold, note); saving shows the result
/// recomputed by the server. Other states are read-only with the actions that
/// fit them: Process, Reopen, Publish, Revise, Delete.
class PayslipEditScreen extends ConsumerWidget {
  const PayslipEditScreen({required this.payslipId, super.key});
  final String payslipId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!canOpenPayroll(perms)) {
      return const Scaffold(body: EmptyView(message: 'You do not have permission to view this.'));
    }
    final async = ref.watch(adminPayslipProvider(payslipId));
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Payslip')),
        body: async.when(
          skipLoadingOnReload: true,
          loading: () => const LoadingView(label: 'Loading payslip'),
          error: (e, _) => ErrorView(
            message: payrollMsg(e),
            onRetry: () => ref.invalidate(adminPayslipProvider(payslipId)),
          ),
          data: (p) => PayslipEditor(
            key: ValueKey('${p.id}-${p.version}-${p.status}-${p.hold}'),
            initial: p,
          ),
        ),
      ),
    );
  }
}

class _AdjRow {
  _AdjRow(this.code, String label, String amount)
      : label = TextEditingController(text: label),
        amount = TextEditingController(text: amount);
  String code;
  final TextEditingController label;
  final TextEditingController amount;

  void dispose() {
    label.dispose();
    amount.dispose();
  }
}

class PayslipEditor extends ConsumerStatefulWidget {
  const PayslipEditor({required this.initial, super.key});
  final Payslip initial;

  @override
  ConsumerState<PayslipEditor> createState() => _PayslipEditorState();
}

class _PayslipEditorState extends ConsumerState<PayslipEditor> {
  final _form = GlobalKey<FormState>();
  late Payslip _p;
  final _lop = TextEditingController();
  final _lopReason = TextEditingController();
  final _tds = TextEditingController();
  final _pt = TextEditingController();
  final _note = TextEditingController();
  final List<_AdjRow> _adj = [];
  bool _hold = false;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _p = widget.initial;
    _fill(_p);
  }

  void _fill(Payslip p) {
    final lop = p.lop;
    final inputs = p.inputs;
    _lop.text = lop?.override == null ? '' : formatDays(lop!.override!);
    _lopReason.text = lop?.overrideReason ?? '';
    _tds.text = inputs?.tds ?? '';
    _pt.text = inputs?.ptOverride ?? '';
    _note.text = inputs?.note ?? '';
    _hold = p.hold;
    for (final r in _adj) {
      r.dispose();
    }
    _adj
      ..clear()
      ..addAll([
        for (final a in inputs?.adjustments ?? const <Adjustment>[])
          _AdjRow(a.code, a.label, a.amount),
      ]);
  }

  @override
  void dispose() {
    for (final c in [_lop, _lopReason, _tds, _pt, _note]) {
      c.dispose();
    }
    for (final r in _adj) {
      r.dispose();
    }
    super.dispose();
  }

  bool get _isSelf {
    final self = ref.read(adminSelfIdProvider);
    return self != null && _p.user?.id == self;
  }

  bool get _editable =>
      _p.isDraft && canManagePayroll(ref.read(adminPermissionsProvider)) && !_isSelf;

  PayslipEditInput _input() => PayslipEditInput(
        lopOverride: parseLop(_lop.text),
        lopReason: _lopReason.text,
        tds: _tds.text,
        ptOverride: _pt.text,
        adjustments: [
          for (final r in _adj)
            Adjustment(code: r.code, label: r.label.text, amount: r.amount.text),
        ],
        hold: _hold,
        note: _note.text,
      );

  Future<void> _save() async {
    if (_busy || !(_form.currentState?.validate() ?? false)) return;
    final patch = buildPayslipPatch(_p, _input());
    if (!patchHasChanges(patch)) {
      payrollSnack(context, 'Nothing changed.');
      return;
    }
    await _run(() async {
      final repo = ref.read(payrollRepositoryProvider);
      final updated = await repo.savePayslip(_p.id, patch);
      if (!mounted) return;
      setState(() {
        _p = updated;
        _fill(updated);
      });
      payrollSnack(context, 'Saved. The amounts below were recalculated.');
    });
  }

  /// Wraps a server call: busy flag and the shared error handling.
  Future<void> _run(Future<void> Function() call) async {
    setState(() => _busy = true);
    try {
      await call();
    } on _Cancelled {
      // the password prompt was closed
    } on ApiException catch (e) {
      if (!mounted) return;
      if (e.code == 'VERSION_CONFLICT') {
        setState(() => _busy = false);
        await showDialog<void>(
          context: context,
          builder: (ctx) => AlertDialog(
            title: const Text('Payslip changed'),
            content: Text(payrollErrorMessage(e)),
            actions: [
              FilledButton(
                onPressed: () => Navigator.pop(ctx),
                style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
                child: const Text('Reload'),
              ),
            ],
          ),
        );
        if (mounted) ref.invalidate(adminPayslipProvider(_p.id));
      } else if (e.code == 'PAYROLL_BLOCKED') {
        unawaited(showBlockedDialog(context, e));
      } else {
        payrollSnack(context, payrollErrorMessage(e));
        // PAYROLL_LOCKED / CONFLICT mean the payslip is no longer in the state this
        // screen shows (13.8): load the current one.
        if (e.code == 'PAYROLL_LOCKED' || e.code == 'CONFLICT') {
          ref.invalidate(adminPayslipProvider(_p.id));
          ref.invalidate(runPayslipsProvider);
        }
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _process() => _run(() async {
        final updated = await ref.read(payrollRepositoryProvider).processPayslip(_p.id);
        _applyServer(updated, 'Payslip processed.');
      });

  Future<void> _reopen() => _run(() async {
        final updated = await ref.read(payrollRepositoryProvider).reopenPayslip(_p.id);
        _applyServer(updated, 'Payslip reopened as draft.');
      });

  void _applyServer(Payslip updated, String message) {
    ref.invalidate(adminPayslipProvider(_p.id));
    ref.invalidate(runPayslipsProvider);
    if (!mounted) return;
    setState(() {
      _p = updated;
      _fill(updated);
    });
    payrollSnack(context, message);
  }

  Future<void> _publish() async {
    final ok = await confirmAction(
      context,
      title: 'Publish this payslip?',
      message: 'The employee is notified and can see it at once. '
          '${_p.revision > 1 ? 'It replaces the earlier revision. ' : ''}'
          'It cannot be edited afterwards, only revised.',
      confirmLabel: 'Publish',
    );
    if (!ok || !mounted) return;
    await _run(() async {
      final repo = ref.read(payrollRepositoryProvider);
      Payslip? published;
      final done = await runWithReauth(
        context,
        reason: 'Publishing a payslip needs your password.',
        action: (pw) async {
          published = await repo.publishPayslip(_p.id, currentPassword: pw);
        },
      );
      if (!done || published == null) throw const _Cancelled();
      _applyServer(published!, 'Payslip published.');
    });
  }

  Future<void> _revise() async {
    final reason = await showReasonDialog(
      context,
      title: 'Revise this payslip',
      hint: 'Reason for the revision (3 to 200 characters)',
      confirmLabel: 'Create revision',
    );
    if (reason == null || !mounted) return;
    await _run(() async {
      final nav = Navigator.of(context);
      final revised = await ref
          .read(payrollRepositoryProvider)
          .revise(_p.runId ?? '', _p.user?.id ?? '', reason);
      ref.invalidate(runPayslipsProvider);
      ref.invalidate(payrollRunProvider);
      unawaited(nav.pushReplacement(
        MaterialPageRoute<void>(builder: (_) => PayslipEditScreen(payslipId: revised.id)),
      ),);
    });
  }

  Future<void> _delete() async {
    final ok = await confirmAction(
      context,
      title: 'Delete this draft payslip?',
      message: 'It is removed from the run. A refresh adds an eligible employee back.',
      confirmLabel: 'Delete',
    );
    if (!ok || !mounted) return;
    await _run(() async {
      final nav = Navigator.of(context);
      await ref.read(payrollRepositoryProvider).deletePayslip(_p.id);
      ref.invalidate(runPayslipsProvider);
      ref.invalidate(payrollRunProvider);
      nav.pop();
    });
  }

  void _addAdjustment() {
    if (_adj.length >= 10) return;
    setState(() => _adj.add(_AdjRow('BONUS', '', '')));
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final perms = ref.watch(adminPermissionsProvider);
    final manage = canManagePayroll(perms);
    final publish = canPublishPayroll(perms);
    final self = _isSelf;
    final u = _p.user;
    final editable = _editable;

    return Form(
      key: _form,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 48),
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  u == null ? 'Payslip' : u.display,
                  style: theme.textTheme.titleLarge,
                ),
              ),
              StatusChip(_p.status),
            ],
          ),
          Text(
            '${periodTitle(_p.period)}${_p.revision > 1 ? '  -  revision ${_p.revision}' : ''}',
            style: theme.textTheme.bodyMedium,
          ),
          const SizedBox(height: 8),
          if (_p.errors.isNotEmpty || _p.warnings.isNotEmpty || _p.hold)
            FlagChips(errors: _p.errors, warnings: _p.warnings, hold: _p.hold),
          if (self)
            const Padding(
              padding: EdgeInsets.only(top: 8),
              child: Text('This is your own payslip. You cannot change it.'),
            ),
          if (_p.revisionInfo?.reason != null)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text('Revision reason: ${_p.revisionInfo!.reason}'),
            ),
          const SizedBox(height: 8),
          if (_p.lop != null) _lopCard(editable),
          if (editable) ..._inputCards(),
          if (_p.totals != null || _p.earnings.isNotEmpty) PayslipView(payslip: _p),
          const SizedBox(height: 12),
          if (editable)
            FilledButton.icon(
              key: const ValueKey('save-payslip'),
              style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
              onPressed: _busy ? null : _save,
              icon: _busy
                  ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.save_outlined),
              label: const Text('Save and recalculate'),
            ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              if (manage && _p.isDraft && !self)
                OutlinedButton.icon(
                  key: const ValueKey('process-payslip'),
                  onPressed: _busy ? null : _process,
                  icon: const Icon(Icons.calculate_outlined),
                  label: const Text('Process'),
                ),
              if (manage && _p.isProcessed && !self)
                OutlinedButton.icon(
                  onPressed: _busy ? null : _reopen,
                  icon: const Icon(Icons.undo),
                  label: const Text('Reopen'),
                ),
              if (publish && _p.isProcessed && !self)
                FilledButton.icon(
                  key: const ValueKey('publish-payslip'),
                  onPressed: _busy ? null : _publish,
                  icon: const Icon(Icons.send_outlined),
                  label: const Text('Publish'),
                ),
              if (manage && _p.isPublished && !self)
                FilledButton.tonalIcon(
                  key: const ValueKey('revise-payslip'),
                  onPressed: _busy ? null : _revise,
                  icon: const Icon(Icons.edit_note),
                  label: const Text('Revise'),
                ),
              if (manage && _p.isDraft && !self)
                TextButton.icon(
                  onPressed: _busy ? null : _delete,
                  icon: const Icon(Icons.delete_outline),
                  label: const Text('Delete'),
                ),
              OutlinedButton.icon(
                onPressed: _busy
                    ? null
                    : () => unawaited(
                          downloadPayrollFile(context, ref, (repo) => repo.payslipPdf(_p.id)),
                        ),
                icon: const Icon(Icons.picture_as_pdf_outlined),
                label: Text(_p.isPublished ? 'PDF' : 'Preview PDF'),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _lopCard(bool editable) {
    final lop = _p.lop!;
    final theme = Theme.of(context);
    return SectionCard(
      title: 'Days and loss of pay',
      children: [
        AmountRow(label: 'Paid days', value: formatDays(_p.days.paidDays)),
        AmountRow(
          label: 'LOP (automatic)',
          value: formatDays(lop.auto),
          caption: 'Unpaid leave ${formatDays(lop.unpaidLeave)}, absent ${formatDays(lop.absent)}, '
              'half-day attendance ${formatDays(lop.halfDayAttendance)}'
              '${lop.notEmployed > 0 ? ', not employed ${formatDays(lop.notEmployed)}' : ''}',
        ),
        if (lop.pendingLeaveDays > 0)
          Text(
            '${formatDays(lop.pendingLeaveDays)} day(s) have a pending leave request. '
            'Resolve it, then refresh the run.',
            style: theme.textTheme.bodySmall,
          ),
        if (lop.override != null && !editable)
          AmountRow(
            label: 'LOP override',
            value: formatDays(lop.override!),
            caption: lop.overrideReason,
          ),
        if (editable) ...[
          const SizedBox(height: 8),
          TextFormField(
            key: const ValueKey('lop-override'),
            controller: _lop,
            keyboardType: const TextInputType.numberWithOptions(decimal: true),
            decoration: InputDecoration(
              labelText: 'LOP override (days, optional)',
              helperText: 'Half-day steps, up to ${formatDays(_p.days.employedDays)} days',
              border: const OutlineInputBorder(),
            ),
            onChanged: (_) => setState(() {}),
            validator: (v) => validateLopOverride(v, _p.days.employedDays),
          ),
          if (_lop.text.trim().isNotEmpty) ...[
            const SizedBox(height: 12),
            TextFormField(
              key: const ValueKey('lop-reason'),
              controller: _lopReason,
              maxLength: 200,
              decoration: const InputDecoration(
                labelText: 'Reason for the override',
                border: OutlineInputBorder(),
              ),
              validator: (v) => validateLopReason(v, overrideSet: _lop.text.trim().isNotEmpty),
            ),
          ],
        ],
      ],
    );
  }

  List<Widget> _inputCards() => [
        SectionCard(
          title: 'Deductions input',
          children: [
            const SizedBox(height: 4),
            MoneyField(controller: _tds, label: 'TDS (monthly)'),
            const SizedBox(height: 12),
            MoneyField(
              controller: _pt,
              label: 'Professional tax override (optional)',
              required: false,
              helper: 'Leave empty to use the slab',
            ),
          ],
        ),
        SectionCard(
          title: 'Adjustments',
          children: [
            if (_adj.isEmpty) const Text('No adjustments.'),
            for (var i = 0; i < _adj.length; i++) _adjustmentRow(i),
            Align(
              alignment: Alignment.centerLeft,
              child: TextButton.icon(
                key: const ValueKey('add-adjustment'),
                onPressed: _adj.length >= 10 ? null : _addAdjustment,
                icon: const Icon(Icons.add),
                label: const Text('Add adjustment'),
              ),
            ),
          ],
        ),
        SectionCard(
          title: 'Other',
          children: [
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              title: const Text('Hold this payslip'),
              subtitle: const Text('Held payslips are skipped when the run is published.'),
              value: _hold,
              onChanged: (v) => setState(() => _hold = v),
            ),
            TextFormField(
              controller: _note,
              maxLength: 200,
              maxLines: 2,
              minLines: 1,
              decoration: const InputDecoration(labelText: 'Note (optional)', border: OutlineInputBorder()),
            ),
          ],
        ),
      ];

  Widget _adjustmentRow(int i) {
    final r = _adj[i];
    final isEarning = earningAdjustmentCodes.contains(r.code);
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Column(
        children: [
          Row(
            children: [
              Expanded(
                child: DropdownButtonFormField<String>(
                  initialValue: r.code,
                  isExpanded: true,
                  decoration: const InputDecoration(labelText: 'Type', border: OutlineInputBorder()),
                  items: [
                    for (final c in adjustmentCodes)
                      DropdownMenuItem(value: c, child: Text(adjustmentCodeLabel(c))),
                  ],
                  onChanged: (v) => setState(() => r.code = v ?? r.code),
                ),
              ),
              IconButton(
                tooltip: 'Remove adjustment',
                icon: const Icon(Icons.delete_outline),
                onPressed: () => setState(() {
                  _adj.removeAt(i).dispose();
                }),
              ),
            ],
          ),
          const SizedBox(height: 8),
          TextFormField(
            controller: r.label,
            maxLength: 40,
            decoration: InputDecoration(
              labelText: isEarning ? 'Label (shown as an earning)' : 'Label (shown as a deduction)',
              border: const OutlineInputBorder(),
            ),
            validator: (v) => validatePayrollLabel(v, label: 'Label'),
          ),
          MoneyField(controller: r.amount, label: 'Amount'),
        ],
      ),
    );
  }
}

class _Cancelled implements Exception {
  const _Cancelled();
}
