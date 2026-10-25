import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/domain/admin_models.dart' show AdminPerm;
import '../../admin/presentation/admin_widgets.dart' show adminPermissionsProvider, runWithReauth;
import '../data/payroll_repository.dart';
import '../domain/payroll_logic.dart';
import '../domain/payroll_models.dart';
import '../domain/payroll_money.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';
import 'pt_slab_editor.dart';

/// Statutory rules and payslip options (`payroll.*` settings). Readable with
/// `payroll.view_all`, editable with `payroll.manage`. Changes apply to new
/// runs and to a draft run after "Refresh" (runs snapshot the settings).
const String secondPersonKey = 'payroll.publish_requires_second_person';

class PayrollSettingsTab extends ConsumerWidget {
  const PayrollSettingsTab({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final perms = ref.watch(adminPermissionsProvider);
    final async = ref.watch(payrollSettingsProvider);
    return async.when(
      skipLoadingOnReload: true,
      loading: () => const LoadingView(label: 'Loading settings'),
      error: (e, _) => ErrorView(
        message: payrollMsg(e),
        onRetry: () => ref.invalidate(payrollSettingsProvider),
      ),
      data: (values) => PayrollSettingsForm(
        initial: values,
        canEdit: canManagePayroll(perms),
        canChangeSecondPerson: perms.contains(AdminPerm.roleAssign),
        onSave: (changes) async {
          final repo = ref.read(payrollRepositoryProvider);
          try {
            if (changes.containsKey(secondPersonKey)) {
              // Flipping maker-checker needs role.assign AND the caller's password (13.3).
              final done = await runWithReauth(
                context,
                reason: 'Changing who may publish payroll needs your password.',
                action: (pw) async {
                  await repo.saveSettings(changes, currentPassword: pw);
                },
              );
              if (!done) {
                return const ApiException(code: 'CANCELLED', message: 'Not saved: the password was not entered.');
              }
            } else {
              await repo.saveSettings(changes);
            }
            ref.invalidate(payrollSettingsProvider);
            return null;
          } on ApiException catch (e) {
            return e;
          }
        },
      ),
    );
  }
}

/// Returns null on success or the [ApiException]; server field errors
/// (`details[].field` = setting key) are shown under the field.
class PayrollSettingsForm extends StatefulWidget {
  const PayrollSettingsForm({
    required this.initial,
    required this.onSave,
    required this.canEdit,
    super.key,
    this.canChangeSecondPerson = true,
  });
  final Map<String, Object?> initial;
  final bool canEdit;

  /// Holds `role.assign`: only then may the maker-checker switch be changed.
  final bool canChangeSecondPerson;
  final Future<ApiException?> Function(Map<String, Object?> changes) onSave;

  @override
  State<PayrollSettingsForm> createState() => _PayrollSettingsFormState();
}

class _PayrollSettingsFormState extends State<PayrollSettingsForm> {
  static const _ptKey = 'payroll.pt_slabs';
  final _form = GlobalKey<FormState>();
  final Map<String, TextEditingController> _text = {};
  final Map<String, bool> _bools = {};
  final Map<String, String> _choices = {};
  final Map<String, String> _serverErrors = {};
  late List<PtSlab> _slabs = PtSlab.parseList(widget.initial[_ptKey]);
  String? _slabError;
  bool _saving = false;
  String? _error;

  List<PayrollSettingSpec> get _specs =>
      [for (final s in payrollSettingSpecs) if (widget.initial.containsKey(s.key)) s];

  @override
  void initState() {
    super.initState();
    for (final s in _specs) {
      final v = widget.initial[s.key];
      switch (s.kind) {
        case PayrollSettingKind.boolean:
          _bools[s.key] = v == true;
        case PayrollSettingKind.choice:
          _choices[s.key] = '$v';
        case PayrollSettingKind.number:
          _text[s.key] = TextEditingController(text: payrollNumberText(v));
        case PayrollSettingKind.text:
          _text[s.key] = TextEditingController(text: '${v ?? ''}');
      }
    }
  }

  @override
  void dispose() {
    for (final c in _text.values) {
      c.dispose();
    }
    super.dispose();
  }

  Map<String, Object?> _edited() => {
        for (final s in _specs)
          s.key: switch (s.kind) {
            PayrollSettingKind.boolean => _bools[s.key],
            PayrollSettingKind.choice => _choices[s.key],
            PayrollSettingKind.number => parsePayrollNumber(_text[s.key]!.text),
            PayrollSettingKind.text => _text[s.key]!.text.trim(),
          },
      };

  Future<void> _save() async {
    setState(() {
      _serverErrors.clear();
      _error = null;
      _slabError = null;
    });
    if (!(_form.currentState?.validate() ?? false)) return;
    final hasSlabs = widget.initial.containsKey(_ptKey);
    if (hasSlabs) {
      final e = validatePtSlabs(_slabs);
      if (e != null) {
        setState(() => _slabError = e);
        return;
      }
    }
    final edited = _edited();
    final changes = changedPayrollSettings(widget.initial, edited);
    if (hasSlabs) {
      final before = jsonEncode([
        for (final s in PtSlab.parseList(widget.initial[_ptKey])) _normalised(s).toJson(),
      ]);
      final after = jsonEncode([for (final s in _slabs) _normalised(s).toJson()]);
      if (before != after) changes[_ptKey] = [for (final s in _slabs) _normalised(s).toJson()];
    }
    if (changes.isEmpty) {
      payrollSnack(context, 'Nothing changed.');
      return;
    }
    setState(() => _saving = true);
    final err = await widget.onSave(changes);
    if (!mounted) return;
    if (err == null) {
      setState(() => _saving = false);
      payrollSnack(context, 'Settings saved. A draft run picks them up after Refresh.');
      return;
    }
    setState(() {
      _saving = false;
      for (final d in err.details) {
        if (d.field.isNotEmpty) _serverErrors[d.field] = d.message;
      }
      _error = payrollErrorMessage(err);
    });
    _form.currentState?.validate();
  }

  static PtSlab _normalised(PtSlab s) => PtSlab(
        minGross: normalizeMoneyOr(s.minGross),
        maxGross: s.maxGross == null || s.maxGross!.isEmpty ? null : normalizeMoneyOr(s.maxGross!),
        amount: normalizeMoneyOr(s.amount),
        months: s.months == null ? null : (s.months!.toList()..sort()),
      );

  @override
  Widget build(BuildContext context) {
    final specs = _specs;
    if (specs.isEmpty) {
      return const EmptyView(
        icon: Icons.settings_outlined,
        message: 'No payroll settings were returned.',
      );
    }
    final groups = <String, List<PayrollSettingSpec>>{};
    for (final s in specs) {
      groups.putIfAbsent(s.group, () => []).add(s);
    }
    final en = widget.canEdit && !_saving;
    final theme = Theme.of(context);
    return Form(
      key: _form,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 48),
        children: [
          if (!widget.canEdit)
            const Padding(
              padding: EdgeInsets.only(bottom: 8),
              child: Text('You can view these settings but not change them.'),
            ),
          for (final g in groups.entries) ...[
            Padding(
              padding: const EdgeInsets.only(top: 8, bottom: 8),
              child: Semantics(header: true, child: Text(g.key, style: theme.textTheme.titleMedium)),
            ),
            if (g.key == 'Employer share')
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Text(
                  'When on, the employee pays the employee share plus the employer share of PF and '
                  'ESI. The payslip shows both as separate deduction lines and net pay is reduced by '
                  'both. The company still remits the employer share, so its net cost for it is zero. '
                  'When off, only the employee share is deducted.',
                  style: theme.textTheme.bodySmall,
                ),
              ),
            if (g.key == 'ESI')
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Text(
                  'ESI applies while the monthly gross is at or below the threshold (default 20,000; '
                  'above it there is no ESI) and stays fixed '
                  'for the Apr-Sep or Oct-Mar period. Contributions are always rounded up to '
                  'the next rupee.',
                  style: theme.textTheme.bodySmall,
                ),
              ),
            for (final s in g.value) Padding(padding: const EdgeInsets.only(bottom: 12), child: _field(s, en)),
            if (g.key == 'Professional tax' && widget.initial.containsKey(_ptKey)) ...[
              Text('Slabs', style: theme.textTheme.titleSmall),
              const SizedBox(height: 4),
              Text(
                'The slab is looked up on the gross rounded up to a whole rupee. Half-yearly: '
                'the gross of the 6 months ending with that month (months not yet in payroll '
                'count as 0), deducted in the months a slab lists; a slab set to every month '
                'is deducted every month. Check the amounts against the current Tamil Nadu '
                'rules before the first real run.',
                style: theme.textTheme.bodySmall,
              ),
              const SizedBox(height: 8),
              PtSlabEditor(
                initial: _slabs,
                enabled: en,
                error: _slabError ?? _serverErrors[_ptKey],
                onChanged: (v) => _slabs = v,
              ),
            ],
          ],
          if (_error != null)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Semantics(
                liveRegion: true,
                child: Text(_error!, style: TextStyle(color: theme.colorScheme.error)),
              ),
            ),
          if (widget.canEdit) ...[
            const SizedBox(height: 16),
            FilledButton(
              key: const ValueKey('save-settings'),
              style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
              onPressed: _saving ? null : _save,
              child: _saving
                  ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Text('Save settings'),
            ),
          ],
        ],
      ),
    );
  }

  Widget _field(PayrollSettingSpec s, bool enabled) {
    switch (s.kind) {
      case PayrollSettingKind.boolean:
        return SwitchListTile(
          contentPadding: EdgeInsets.zero,
          title: Text(s.label),
          subtitle: s.key == secondPersonKey && !widget.canChangeSecondPerson
              ? const Text('Only an administrator can change this.')
              : null,
          value: _bools[s.key]!,
          onChanged: enabled && (s.key != secondPersonKey || widget.canChangeSecondPerson)
              ? (v) => setState(() => _bools[s.key] = v)
              : null,
        );
      case PayrollSettingKind.choice:
        return DropdownButtonFormField<String>(
          initialValue: s.options.any((o) => o.$1 == _choices[s.key]) ? _choices[s.key] : null,
          isExpanded: true,
          decoration: InputDecoration(
            labelText: s.label,
            border: const OutlineInputBorder(),
            errorText: _serverErrors[s.key],
          ),
          items: [
            for (final o in s.options) DropdownMenuItem(value: o.$1, child: Text(o.$2)),
          ],
          onChanged: enabled ? (v) => setState(() => _choices[s.key] = v ?? _choices[s.key]!) : null,
        );
      case PayrollSettingKind.number:
        return TextFormField(
          controller: _text[s.key],
          enabled: enabled,
          keyboardType: const TextInputType.numberWithOptions(decimal: true),
          inputFormatters: [FilteringTextInputFormatter.allow(RegExp(r'[0-9.]'))],
          decoration: InputDecoration(
            labelText: s.label,
            suffixText: s.suffix,
            border: const OutlineInputBorder(),
            errorText: _serverErrors[s.key],
          ),
          validator: (v) => validatePayrollNumber(v, s),
        );
      case PayrollSettingKind.text:
        return TextFormField(
          controller: _text[s.key],
          enabled: enabled,
          maxLength: s.maxLength,
          maxLines: 3,
          minLines: 1,
          decoration: InputDecoration(
            labelText: s.label,
            border: const OutlineInputBorder(),
            errorText: _serverErrors[s.key],
          ),
        );
    }
  }
}

/// Money text in the wire format when valid, otherwise unchanged.
String normalizeMoneyOr(String v) => normalizeMoney(v) ?? v;
