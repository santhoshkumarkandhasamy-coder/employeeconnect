import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef, NamedRef;
import '../../admin/presentation/admin_providers.dart' show departmentRefsProvider;
import '../data/payroll_repository.dart';

import '../domain/payroll_models.dart' show payrollErrorMessage;
import '../domain/phase4a_models.dart';
import 'batch_screens.dart';
import 'increment_widgets.dart';
import 'pay_items_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

class _Override {
  _Override(this.user, String percent) : percent = TextEditingController(text: percent);
  final EmployeeRef user;
  final TextEditingController percent;
}

/// Bulk hike (contract 18.5): effective month, percent, method, scope and
/// per-employee percent overrides. Creating it makes one DRAFT increment per
/// employee in a batch; nothing is submitted or approved here. The result
/// lists the employees that were skipped and why.
class BulkHikeWizardScreen extends ConsumerStatefulWidget {
  const BulkHikeWizardScreen({super.key});

  @override
  ConsumerState<BulkHikeWizardScreen> createState() => _BulkHikeWizardScreenState();
}

class _BulkHikeWizardScreenState extends ConsumerState<BulkHikeWizardScreen> {
  final _form = GlobalKey<FormState>();
  final _percent = TextEditingController();
  final _reason = TextEditingController();
  String? _month;
  String _method = 'proportional';
  final Set<String> _departments = {};
  final List<EmployeeRef> _only = [];
  final List<EmployeeRef> _exclude = [];
  final List<_Override> _overrides = [];
  bool _saving = false;
  BulkHikeResult? _result;

  @override
  void dispose() {
    _percent.dispose();
    _reason.dispose();
    for (final o in _overrides) {
      o.percent.dispose();
    }
    super.dispose();
  }

  Future<EmployeeRef?> _pick() =>
      showDialog<EmployeeRef>(context: context, builder: (_) => const EmployeePickerDialog());

  Future<void> _create(int maxPct) async {
    if (_saving) return;
    if (!(_form.currentState?.validate() ?? false)) return;
    final overrides = <String, String>{
      for (final o in _overrides) o.user.id: normalizePercent(o.percent.text) ?? o.percent.text.trim(),
    };
    final input = BulkHikeInput(
      effectiveFrom: '$_month-01',
      percent: normalizePercent(_percent.text) ?? _percent.text.trim(),
      method: _method,
      reason: _reason.text.trim(),
      departmentIds: _departments.toList(),
      userIds: [for (final u in _only) u.id],
      excludeUserIds: [for (final u in _exclude) u.id],
      overrides: overrides,
    );
    setState(() => _saving = true);
    final repo = ref.read(payrollRepositoryProvider);
    try {
      final r = await repo.bulkHike(input);
      invalidateIncrementState(ref);
      if (mounted) setState(() => _result = r);
    } on ApiException catch (e) {
      if (mounted) payrollSnack(context, payrollErrorMessage(e));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final maxPct = ref.watch(incrementMaxPctProvider).valueOrNull ?? 50;
    final thisMonth = monthKey(ref.read(payrollClockProvider)());
    _month ??= shiftMonth(thisMonth, 1);
    final r = _result;
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Bulk hike')),
        body: r != null ? _resultView(r) : _formView(maxPct, thisMonth),
      ),
    );
  }

  Widget _resultView(BulkHikeResult r) {
    final theme = Theme.of(context);
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 48),
      children: [
        Card(
          key: const ValueKey('hike-created'),
          color: theme.colorScheme.primaryContainer,
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Text(
              '${r.created} draft increments created for ${effectiveLabel(r.batch.effectiveFrom)}. '
              'Review them, then submit the batch for approval.',
              style: TextStyle(color: theme.colorScheme.onPrimaryContainer),
            ),
          ),
        ),
        SectionCard(
          title: 'Totals',
          children: [
            AmountRow(label: 'Monthly gross now', value: formatBigRupees(r.batch.totals.oldGross)),
            AmountRow(label: 'Monthly gross after', value: formatBigRupees(r.batch.totals.newGross)),
            AmountRow(label: 'Extra per month', value: formatBigRupees(r.batch.totals.deltaMonthly), bold: true),
            AmountRow(label: 'Extra per year', value: formatBigRupees(r.batch.totals.deltaAnnual)),
          ],
        ),
        if (r.skipped.isNotEmpty)
          SectionCard(
            title: 'Skipped (${r.skipped.length})',
            children: [
              for (final s in r.skipped)
                ListTile(
                  key: ValueKey('skipped-${s.user.id}'),
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  leading: const Icon(Icons.info_outline),
                  title: Text(s.user.display),
                  subtitle: Text(bulkReasonLabel(s.reason)),
                ),
            ],
          ),
        const SizedBox(height: 8),
        FilledButton(
          key: const ValueKey('hike-review'),
          style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
          onPressed: () => Navigator.of(context).pushReplacement(
            MaterialPageRoute<void>(builder: (_) => BatchDetailScreen(batchId: r.batch.id)),
          ),
          child: const Text('Review the batch'),
        ),
      ],
    );
  }

  Widget _formView(int maxPct, String thisMonth) {
    final theme = Theme.of(context);
    final depts = ref.watch(departmentRefsProvider);
    return Form(
      key: _form,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 48),
        children: [
          Text(
            'Raise the salary of many employees by the same percent. This creates one draft '
            'increment for each employee. Nothing changes until the batch is submitted and '
            'another person approves it.',
            style: theme.textTheme.bodySmall,
          ),
          const SizedBox(height: 12),
          MonthDropdown(
            label: 'Effective from (month)',
            value: _month,
            options: monthOptionsWith(thisMonth, _month),
            onChanged: (v) => setState(() => _month = v ?? _month),
          ),
          const SizedBox(height: 12),
          TextFormField(
            key: const ValueKey('hike-percent'),
            controller: _percent,
            keyboardType: const TextInputType.numberWithOptions(decimal: true),
            inputFormatters: [FilteringTextInputFormatter.allow(RegExp(r'[0-9.]'))],
            decoration: InputDecoration(
              labelText: 'Increase (%)',
              suffixText: '%',
              helperText: 'Up to $maxPct%',
              border: const OutlineInputBorder(),
            ),
            validator: (v) => validatePercent(v, maxPct: maxPct),
          ),
          const SizedBox(height: 12),
          DropdownButtonFormField<String>(
            key: const ValueKey('hike-method'),
            initialValue: _method,
            isExpanded: true,
            decoration: const InputDecoration(labelText: 'How to apply', border: OutlineInputBorder()),
            items: const [
              DropdownMenuItem(value: 'proportional', child: Text('Scale every component')),
              DropdownMenuItem(value: 'split_from_gross', child: Text('Split from the new gross')),
            ],
            onChanged: (v) => setState(() => _method = v ?? _method),
          ),
          const SizedBox(height: 12),
          TextFormField(
            key: const ValueKey('hike-reason'),
            controller: _reason,
            maxLength: 200,
            maxLines: 2,
            minLines: 1,
            decoration: const InputDecoration(
              labelText: 'Reason',
              hintText: 'Annual increment FY 2027-28',
              border: OutlineInputBorder(),
            ),
            validator: (v) => validateComment(v, required: true)?.replaceFirst('A comment', 'A reason'),
          ),
          const SizedBox(height: 16),
          Text('Who is included', style: theme.textTheme.titleSmall),
          Text(
            'Leave everything empty to include every active employee with a salary structure '
            '(at most 500).',
            style: theme.textTheme.bodySmall,
          ),
          const SizedBox(height: 8),
          depts.when(
            loading: () => const LinearProgressIndicator(),
            error: (e, _) => const Text('Departments could not be loaded. You can still pick employees.'),
            data: (list) => Wrap(
              spacing: 8,
              runSpacing: 4,
              children: [
                for (final NamedRef d in list)
                  FilterChip(
                    key: ValueKey('hike-dept-${d.id}'),
                    label: Text(d.name),
                    selected: _departments.contains(d.id),
                    onSelected: (v) => setState(() => v ? _departments.add(d.id) : _departments.remove(d.id)),
                  ),
              ],
            ),
          ),
          const SizedBox(height: 8),
          _people('Only these employees', 'hike-only', _only),
          _people('Leave out', 'hike-exclude', _exclude),
          const SizedBox(height: 8),
          Row(
            children: [
              Expanded(child: Text('Different percent for some', style: theme.textTheme.titleSmall)),
              TextButton.icon(
                key: const ValueKey('hike-add-override'),
                onPressed: () async {
                  final u = await _pick();
                  if (u == null || !mounted || _overrides.any((o) => o.user.id == u.id)) return;
                  setState(() => _overrides.add(_Override(u, _percent.text.trim())));
                },
                icon: const Icon(Icons.add),
                label: const Text('Add'),
              ),
            ],
          ),
          for (var i = 0; i < _overrides.length; i++)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 4),
              child: Row(
                children: [
                  Expanded(child: Text(_overrides[i].user.display, overflow: TextOverflow.ellipsis)),
                  const SizedBox(width: 8),
                  SizedBox(
                    width: 100,
                    child: TextFormField(
                      key: ValueKey('hike-override-$i'),
                      controller: _overrides[i].percent,
                      keyboardType: const TextInputType.numberWithOptions(decimal: true),
                      inputFormatters: [FilteringTextInputFormatter.allow(RegExp(r'[0-9.]'))],
                      decoration: const InputDecoration(suffixText: '%', isDense: true, border: OutlineInputBorder()),
                      validator: (v) => validatePercent(v, maxPct: maxPct),
                    ),
                  ),
                  IconButton(
                    tooltip: 'Remove',
                    icon: const Icon(Icons.close),
                    onPressed: () => setState(() => _overrides.removeAt(i).percent.dispose()),
                  ),
                ],
              ),
            ),
          const SizedBox(height: 16),
          FilledButton(
            key: const ValueKey('hike-create'),
            style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
            onPressed: _saving ? null : () => _create(maxPct),
            child: _saving
                ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('Create draft increments'),
          ),
        ],
      ),
    );
  }

  Widget _people(String title, String keyPrefix, List<EmployeeRef> list) => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(child: Text(title, style: Theme.of(context).textTheme.bodyMedium)),
              TextButton.icon(
                key: ValueKey('$keyPrefix-add'),
                onPressed: () async {
                  final u = await _pick();
                  if (u == null || !mounted || list.any((x) => x.id == u.id)) return;
                  setState(() => list.add(u));
                },
                icon: const Icon(Icons.add),
                label: const Text('Add'),
              ),
            ],
          ),
          if (list.isNotEmpty)
            Wrap(
              spacing: 6,
              runSpacing: 4,
              children: [
                for (final u in list)
                  InputChip(
                    label: Text(u.display),
                    onDeleted: () => setState(() => list.removeWhere((x) => x.id == u.id)),
                  ),
              ],
            ),
        ],
      );
}
