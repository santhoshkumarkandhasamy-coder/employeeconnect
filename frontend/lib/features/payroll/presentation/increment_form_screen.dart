import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../data/payroll_repository.dart';
import '../domain/payroll_models.dart';
import '../domain/payroll_money.dart';
import '../domain/phase4a_models.dart';
import 'increment_detail_screen.dart';
import 'increment_widgets.dart';
import 'pay_items_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';
import 'structure_form_screen.dart';

/// Create or edit (draft) one increment (contract 18.4). Kinds: percent,
/// new gross, full structure (reuses the structure form) and remove. The
/// server builds the exact proposed structure and returns the diff and the
/// warnings; they are shown on the detail screen right after saving. The
/// preview here is only an estimate.
class IncrementFormScreen extends ConsumerStatefulWidget {
  const IncrementFormScreen({
    required this.userId,
    super.key,
    this.userName,
    this.existing,
    this.initialStructure,
  });
  final String userId;
  final String? userName;

  /// A draft to edit (the kind cannot change).
  final Increment? existing;

  /// Prefill kind "full structure" (from the CTC calculator).
  final StructureInput? initialStructure;

  @override
  ConsumerState<IncrementFormScreen> createState() => _IncrementFormScreenState();
}

class _IncrementFormScreenState extends ConsumerState<IncrementFormScreen> {
  final _form = GlobalKey<FormState>();
  final _percent = TextEditingController();
  final _gross = TextEditingController();
  final _reason = TextEditingController();
  late String _kind = widget.existing?.kind ?? (widget.initialStructure != null ? 'structure' : 'percent');
  late String _method = widget.existing?.method ?? 'proportional';
  String? _month; // YYYY-MM
  StructureInput? _structure;
  String? _removeId;
  String? _replacesId;
  bool _saving = false;
  String? _structureError;

  bool get _editing => widget.existing != null;

  @override
  void initState() {
    super.initState();
    _structure = widget.initialStructure;
    final e = widget.existing;
    if (e != null) {
      _percent.text = e.percent ?? '';
      _reason.text = e.reason;
      _month = e.effectiveFrom?.substring(0, 7);
      if (e.kind == 'amount') _gross.text = e.proposed?.monthlyGross ?? e.change.newGross ?? '';
      if (e.kind == 'remove') _removeId = e.replacesStructureId;
      if (e.kind == 'structure') {
        _replacesId = e.replacesStructureId;
        final p = e.proposedAsStructure();
        if (p != null) {
          _structure = StructureInput(
            effectiveFrom: e.effectiveFrom ?? '',
            basic: p.basic,
            hra: p.hra,
            allowances: p.allowances,
            fixedDeductions: p.fixedDeductions,
            tdsMonthly: p.tdsMonthly,
            pfApplicable: p.pfApplicable,
            pfOnFullBasic: p.pfOnFullBasic,
            esiApplicable: p.esiApplicable,
            ptApplicable: p.ptApplicable,
          );
        }
      }
    }
  }

  @override
  void dispose() {
    _percent.dispose();
    _gross.dispose();
    _reason.dispose();
    super.dispose();
  }

  String get _thisMonth => monthKey(ref.read(payrollClockProvider)());

  /// The structure in force on [month] (`YYYY-MM`): latest start on or before it.
  SalaryStructure? _inForce(List<SalaryStructure> all, String? month) {
    if (month == null) return null;
    SalaryStructure? best;
    for (final s in all) {
      if (s.effectiveFrom.substring(0, 7).compareTo(month) <= 0 &&
          (best == null || s.effectiveFrom.compareTo(best.effectiveFrom) > 0)) {
        best = s;
      }
    }
    return best;
  }

  Future<void> _pickStructure(List<SalaryStructure> all) async {
    final base = _inForce(all, _month) ?? (all.isEmpty ? null : all.first);
    final picked = await Navigator.of(context).push<StructureInput>(
      MaterialPageRoute<StructureInput>(
        builder: (_) => StructureFormScreen(
          userId: widget.userId,
          copyFrom: _structure == null ? base : salaryStructureFromInput(widget.userId, _structure!),
          returnInput: true,
        ),
      ),
    );
    if (picked == null || !mounted) return;
    setState(() {
      _structure = picked;
      _structureError = null;
      // A structure on the same date as an existing one can only replace it.
      final same = all.where((s) => s.effectiveFrom == picked.effectiveFrom);
      _replacesId = same.isEmpty ? null : same.first.id;
    });
  }

  IncrementInput? _buildInput() {
    final reason = _reason.text.trim();
    switch (_kind) {
      case 'percent':
        return IncrementInput.percent(
          effectiveFrom: '$_month-01',
          percent: normalizePercent(_percent.text) ?? _percent.text.trim(),
          method: _method,
          reason: reason,
        );
      case 'amount':
        return IncrementInput.amount(
          effectiveFrom: '$_month-01',
          monthlyGross: normalizeMoney(_gross.text) ?? _gross.text.trim(),
          method: _method,
          reason: reason,
        );
      case 'structure':
        final s = _structure;
        if (s == null) return null;
        return IncrementInput.structure(structure: s, replacesStructureId: _replacesId, reason: reason);
      case 'remove':
        return IncrementInput.remove(replacesStructureId: _removeId ?? '', reason: reason);
    }
    return null;
  }

  Future<void> _save() async {
    if (_saving) return;
    setState(() => _structureError = null);
    if (!(_form.currentState?.validate() ?? false)) return;
    if (_kind == 'structure' && _structure == null) {
      setState(() => _structureError = 'Set up the new structure first');
      return;
    }
    final input = _buildInput();
    if (input == null) return;
    setState(() => _saving = true);
    final repo = ref.read(payrollRepositoryProvider);
    final nav = Navigator.of(context);
    try {
      if (_editing) {
        await repo.updateIncrement(widget.existing!.id, input, expectedVersion: widget.existing!.version);
        invalidateIncrementState(ref, incrementId: widget.existing!.id, userId: widget.userId);
        nav.pop(true);
      } else {
        final created = await repo.createIncrement(widget.userId, input);
        invalidateIncrementState(ref, userId: widget.userId);
        await nav.pushReplacement(
          MaterialPageRoute<void>(builder: (_) => IncrementDetailScreen(incrementId: created.id)),
        );
      }
    } on ApiException catch (e) {
      if (isStaleIncrementError(e) && _editing) {
        invalidateIncrementState(ref, incrementId: widget.existing!.id, userId: widget.userId);
      }
      if (mounted) payrollSnack(context, payrollErrorMessage(e));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final structures = ref.watch(payrollStructuresProvider(widget.userId));
    final settings = ref.watch(payrollSettingsProvider).valueOrNull;
    final maxPct = ref.watch(incrementMaxPctProvider).valueOrNull ?? 50;
    final backdateRaw = settings?['payroll.increment_backdate_months'];
    final backdate = backdateRaw is num ? backdateRaw.toInt() : 12;
    final all = structures.valueOrNull ?? const <SalaryStructure>[];
    _month ??= shiftMonth(_thisMonth, 1);
    final months = monthOptionsWith(_thisMonth, _month, back: backdate);
    final inForce = _inForce(all, _month);

    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: Text(_editing ? 'Edit increment' : 'New increment')),
        body: Form(
          key: _form,
          child: ListView(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 48),
            children: [
              if (widget.userName != null && widget.userName!.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(bottom: 8),
                  child: Text(widget.userName!, style: theme.textTheme.titleMedium),
                ),
              Text(
                'An increment is prepared here, submitted, and approved by another person '
                'before it becomes the new salary structure.',
                style: theme.textTheme.bodySmall,
              ),
              const SizedBox(height: 12),
              if (_editing)
                Text('Kind: ${incrementKindLabel(_kind)}', style: theme.textTheme.titleSmall)
              else
                Wrap(
                  spacing: 8,
                  runSpacing: 4,
                  children: [
                    for (final k in const ['percent', 'amount', 'structure', 'remove'])
                      ChoiceChip(
                        key: ValueKey('kind-$k'),
                        label: Text(incrementKindLabel(k)),
                        selected: _kind == k,
                        onSelected: (_) => setState(() => _kind = k),
                      ),
                  ],
                ),
              const SizedBox(height: 16),
              if (_kind == 'percent' || _kind == 'amount') ...[
                MonthDropdown(
                  label: 'Effective from (month)',
                  value: _month,
                  options: months,
                  onChanged: (v) => setState(() => _month = v ?? _month),
                ),
                const SizedBox(height: 4),
                Text(
                  'Backdated months are allowed up to $backdate months; arrears are then proposed.',
                  style: theme.textTheme.bodySmall,
                ),
                const SizedBox(height: 12),
              ],
              if (_kind == 'percent') ...[
                TextFormField(
                  key: const ValueKey('inc-percent'),
                  controller: _percent,
                  keyboardType: const TextInputType.numberWithOptions(decimal: true),
                  inputFormatters: [FilteringTextInputFormatter.allow(RegExp(r'[0-9.]'))],
                  onChanged: (_) => setState(() {}),
                  decoration: InputDecoration(
                    labelText: 'Increase (%)',
                    suffixText: '%',
                    helperText: 'Up to $maxPct%. A decrease needs a full structure.',
                    border: const OutlineInputBorder(),
                  ),
                  validator: (v) => validatePercent(v, maxPct: maxPct),
                ),
                const SizedBox(height: 8),
                _percentPreview(inForce),
              ],
              if (_kind == 'amount') ...[
                MoneyField(
                  key: const ValueKey('inc-gross'),
                  controller: _gross,
                  label: 'New monthly gross',
                  positive: true,
                  onChanged: () => setState(() {}),
                ),
                const SizedBox(height: 4),
                if (inForce != null)
                  Text('Now ${formatRupees(inForce.monthlyGross)}', style: theme.textTheme.bodySmall),
              ],
              if (_kind == 'percent' || _kind == 'amount') ...[
                const SizedBox(height: 12),
                DropdownButtonFormField<String>(
                  key: const ValueKey('inc-method'),
                  initialValue: _method,
                  isExpanded: true,
                  decoration: const InputDecoration(labelText: 'How to apply', border: OutlineInputBorder()),
                  items: [
                    DropdownMenuItem(
                      value: 'proportional',
                      child: Text(_kind == 'percent' ? 'Scale every component' : 'Scale the current components'),
                    ),
                    const DropdownMenuItem(value: 'split_from_gross', child: Text('Split from the new gross')),
                  ],
                  onChanged: (v) => setState(() => _method = v ?? _method),
                ),
              ],
              if (_kind == 'structure') _structureBlock(all),
              if (_kind == 'remove') _removeBlock(all),
              const SizedBox(height: 16),
              TextFormField(
                key: const ValueKey('inc-reason'),
                controller: _reason,
                maxLength: 200,
                maxLines: 3,
                minLines: 1,
                decoration: const InputDecoration(labelText: 'Reason', border: OutlineInputBorder()),
                validator: (v) => validateComment(v, required: true)?.replaceFirst('A comment', 'A reason'),
              ),
              const SizedBox(height: 16),
              FilledButton(
                key: const ValueKey('inc-save'),
                style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
                onPressed: _saving ? null : _save,
                child: _saving
                    ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                    : Text(_editing ? 'Save changes' : 'Save as draft'),
              ),
              const SizedBox(height: 6),
              Text(
                'After saving you see the exact change and any warnings, then submit it.',
                style: theme.textTheme.bodySmall,
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _percentPreview(SalaryStructure? inForce) {
    final p = normalizePercent(_percent.text);
    if (inForce == null || p == null) return const SizedBox.shrink();
    final est = previewPercentGross(inForce.monthlyGross, p);
    if (est == null) return const SizedBox.shrink();
    return Text(
      'Estimate: ${formatRupees(inForce.monthlyGross)} to ${formatRupees(est)} a month. '
      'The exact figures come back after saving.',
      key: const ValueKey('inc-preview'),
      style: Theme.of(context).textTheme.bodySmall,
    );
  }

  Widget _structureBlock(List<SalaryStructure> all) {
    final theme = Theme.of(context);
    final s = _structure;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        OutlinedButton.icon(
          key: const ValueKey('inc-structure'),
          onPressed: () => _pickStructure(all),
          icon: const Icon(Icons.edit_note_outlined),
          label: Text(s == null ? 'Set up the new structure' : 'Change the new structure'),
        ),
        if (s != null) ...[
          const SizedBox(height: 6),
          Text(
            'From ${effectiveLabel(s.effectiveFrom)}: gross ${formatRupees(s.monthlyGross)}',
            key: const ValueKey('inc-structure-summary'),
          ),
          if (_replacesId != null)
            Text(
              'A structure already starts that month, so this replaces it.',
              style: theme.textTheme.bodySmall,
            ),
        ],
        if (_structureError != null)
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Text(_structureError!, style: TextStyle(color: theme.colorScheme.error)),
          ),
      ],
    );
  }

  Widget _removeBlock(List<SalaryStructure> all) {
    return DropdownButtonFormField<String>(
      key: const ValueKey('inc-remove'),
      initialValue: all.any((s) => s.id == _removeId) ? _removeId : null,
      isExpanded: true,
      decoration: const InputDecoration(
        labelText: 'Structure to remove',
        helperText: 'Only a structure no payslip uses can be removed',
        border: OutlineInputBorder(),
      ),
      items: [
        for (final s in all)
          DropdownMenuItem(
            value: s.id,
            enabled: !s.inUse,
            child: Text(
              'From ${effectiveLabel(s.effectiveFrom)}: ${formatRupees(s.monthlyGross)}${s.inUse ? ' (in use)' : ''}',
              overflow: TextOverflow.ellipsis,
            ),
          ),
      ],
      onChanged: (v) => setState(() => _removeId = v),
      validator: (v) => v == null ? 'Choose the structure to remove' : null,
    );
  }
}
