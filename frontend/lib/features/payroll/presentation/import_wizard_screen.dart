import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../admin/presentation/admin_widgets.dart' show runWithReauth;
import '../data/import_file_picker.dart';
import '../data/payroll_repository.dart';
import '../domain/pay_items_models.dart';
import '../domain/payroll_models.dart';
import '../domain/payroll_money.dart';
import 'pay_items_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// Bulk one-time items from an Excel file (contract 17.2):
/// 1 download the template, 2 pick the file and check it (dry run, nothing is
/// written), 3 confirm with your password. The commit sends the SHA-256 that
/// the check returned, so a changed file is refused.
class ImportWizardScreen extends ConsumerStatefulWidget {
  const ImportWizardScreen({required this.initialPeriod, super.key});
  final String initialPeriod;

  @override
  ConsumerState<ImportWizardScreen> createState() => _ImportWizardScreenState();
}

/// Shown in the report instead of thousands of rows.
const int _maxErrorRows = 100;

class _ImportWizardScreenState extends ConsumerState<ImportWizardScreen> {
  late String _period = widget.initialPeriod;
  late final String _thisMonth = monthKey(ref.read(payrollClockProvider)());
  PickedWorkbook? _file;
  ImportReport? _report;
  ImportCommit? _done;
  String? _error;
  bool _busy = false;

  Future<void> _template() => downloadPayrollFile(context, ref, (repo) => repo.itemsTemplate());

  Future<void> _pick() async {
    final f = await ref.read(payrollImportPickerProvider).pickWorkbook();
    if (f == null || !mounted) return;
    setState(() {
      _report = null;
      _done = null;
      if (f.tooLarge || f.bytes.length > payrollImportMaxBytes) {
        _file = null;
        _error = 'The file is too large (1 MB at most).';
      } else {
        _file = f;
        _error = null;
      }
    });
  }

  Future<void> _check() async {
    final f = _file;
    if (f == null || _busy) return;
    final repo = ref.read(payrollRepositoryProvider);
    setState(() {
      _busy = true;
      _error = null;
      _report = null;
    });
    try {
      final r = await repo.importDryRun(bytes: f.bytes, filename: f.name, period: _period);
      if (mounted) setState(() => _report = r);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = _importErrorText(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  String _importErrorText(ApiException e) {
    final base = payrollErrorMessage(e);
    // IMPORT_INVALID on commit lists rows as details.
    final rows = [for (final d in e.details) ImportRowError.fromJson({'field': d.field, 'message': d.message})]
        .where((r) => r.row > 0)
        .take(5)
        .map((r) => 'Row ${r.row}: ${importErrorLabel(r.message)}');
    return rows.isEmpty ? base : '$base\n${rows.join('\n')}';
  }

  Future<void> _commit() async {
    final f = _file;
    final r = _report;
    if (f == null || r == null || !r.canCommit || _busy) return;
    if (f.sha256Hex != r.fileSha256) {
      setState(
        () => _error = payrollErrorMessage(const ApiException(code: 'IMPORT_FILE_CHANGED', message: '')),
      );
      return;
    }
    final repo = ref.read(payrollRepositoryProvider);
    ImportCommit? out;
    try {
      final ok = await runWithReauth(
        context,
        reason: 'Importing ${r.valid} pay items for ${periodTitle(r.period)} needs your password.',
        action: (pw) async {
          out = await repo.importCommit(
            bytes: f.bytes,
            filename: f.name,
            period: r.period,
            fileSha256: r.fileSha256,
            currentPassword: pw,
          );
        },
      );
      if (!ok || !mounted) return;
      setState(() {
        _done = out;
        _error = null;
      });
      ref.invalidate(oneTimeItemsProvider);
      ref.invalidate(importBatchesProvider);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = _importErrorText(e));
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final r = _report;
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Import pay items')),
        body: ListView(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 48),
          children: [
            SectionCard(
              title: '1. Template',
              children: [
                const Text(
                  'Fill in one row per item: Employee ID, Code, Label, Amount. '
                  'Delete the example row first. Up to 500 rows and 1 MB.',
                ),
                const SizedBox(height: 8),
                OutlinedButton.icon(
                  key: const ValueKey('import-template'),
                  onPressed: _template,
                  icon: const Icon(Icons.download_outlined),
                  label: const Text('Download template'),
                ),
              ],
            ),
            SectionCard(
              title: '2. Choose the month and file',
              children: [
                const SizedBox(height: 4),
                MonthDropdown(
                  label: 'Month the items are paid in',
                  value: _period,
                  options: monthOptionsWith(_thisMonth, widget.initialPeriod),
                  onChanged: _busy || _done != null
                      ? (_) {}
                      : (v) => setState(() {
                            _period = v ?? _period;
                            _report = null;
                          }),
                ),
                const SizedBox(height: 12),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: [
                    OutlinedButton.icon(
                      key: const ValueKey('import-pick'),
                      onPressed: _busy ? null : _pick,
                      icon: const Icon(Icons.attach_file),
                      label: Text(_file == null ? 'Choose .xlsx file' : 'Choose another file'),
                    ),
                    if (_file != null) Text(_file!.name, key: const ValueKey('import-filename')),
                  ],
                ),
                const SizedBox(height: 12),
                FilledButton.icon(
                  key: const ValueKey('import-check'),
                  onPressed: _file == null || _busy || _done != null ? null : _check,
                  icon: _busy
                      ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                      : const Icon(Icons.fact_check_outlined),
                  label: const Text('Check file'),
                ),
              ],
            ),
            if (_error != null)
              Card(
                key: const ValueKey('import-error'),
                color: theme.colorScheme.errorContainer,
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Text(_error!, style: TextStyle(color: theme.colorScheme.onErrorContainer)),
                ),
              ),
            if (r != null) _reportCard(r),
            if (_done != null)
              Card(
                key: const ValueKey('import-done'),
                color: theme.colorScheme.primaryContainer,
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Text(
                    'Imported ${_done!.created} pay items for ${periodTitle(_done!.period)}. '
                    'You can cancel the batch from "Batches" while the items are pending.',
                    style: TextStyle(color: theme.colorScheme.onPrimaryContainer),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }

  Widget _reportCard(ImportReport r) {
    final theme = Theme.of(context);
    final hint = runStatusHint(r.runStatus);
    final shown = r.errors.take(_maxErrorRows).toList();
    return SectionCard(
      title: '3. Check result',
      children: [
        Text(
          '${r.rows} rows, ${r.valid} valid, ${r.errors.length} with errors',
          key: const ValueKey('import-summary'),
          style: theme.textTheme.titleSmall,
        ),
        if (hint != null) Text('$hint.', style: theme.textTheme.bodySmall),
        const SizedBox(height: 8),
        for (final b in r.byCode)
          AmountRow(
            label: '${oneTimeCodeLabel(b.code)} (${b.count})',
            value: formatRupees(b.total),
          ),
        if (shown.isNotEmpty) ...[
          const Divider(),
          Text('Fix these rows in the file, then choose it again:', style: theme.textTheme.bodySmall),
          for (final e in shown)
            ListTile(
              key: ValueKey('import-row-${e.row}-${e.message}'),
              dense: true,
              contentPadding: EdgeInsets.zero,
              leading: Icon(Icons.error_outline, color: theme.colorScheme.error),
              title: Text('Row ${e.row}: ${importErrorLabel(e.message)}'),
              subtitle: e.field.isEmpty ? null : Text('Column: ${e.field}'),
            ),
          if (r.errors.length > shown.length) Text('and ${r.errors.length - shown.length} more.'),
        ],
        const SizedBox(height: 12),
        FilledButton.icon(
          key: const ValueKey('import-commit'),
          onPressed: r.canCommit && _done == null && !_busy ? _commit : null,
          icon: const Icon(Icons.lock_outline),
          label: Text(r.canCommit ? 'Import ${r.valid} items' : 'Cannot import yet'),
        ),
        if (r.errors.isEmpty && r.valid == 0)
          const Padding(padding: EdgeInsets.only(top: 6), child: Text('The file has no rows to import.')),
      ],
    );
  }
}
