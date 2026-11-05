import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../admin/presentation/admin_widgets.dart' show runWithReauth;
import '../data/import_file_picker.dart';
import '../data/payroll_repository.dart';
import '../domain/payroll_models.dart';
import '../domain/phase4a_models.dart';
import 'increment_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// Bulk salary structures from an Excel file (contract 18.6): 1 download the
/// template, 2 pick the file and check it (dry run, nothing is written),
/// 3 give a reason and confirm with your password. Employees without a
/// structure get one directly; employees who have one get a submitted
/// increment that another person approves.
class StructureImportWizardScreen extends ConsumerStatefulWidget {
  const StructureImportWizardScreen({super.key});

  @override
  ConsumerState<StructureImportWizardScreen> createState() => _StructureImportWizardScreenState();
}

/// Shown in the report instead of hundreds of rows.
const int _maxIssueRows = 100;

class _StructureImportWizardScreenState extends ConsumerState<StructureImportWizardScreen> {
  final _reason = TextEditingController();
  final _reasonKey = GlobalKey<FormFieldState<String>>();
  PickedWorkbook? _file;
  StructureImportReport? _report;
  StructureImportCommit? _done;
  String? _error;
  bool _busy = false;

  @override
  void dispose() {
    _reason.dispose();
    super.dispose();
  }

  Future<void> _template() => downloadPayrollFile(context, ref, (repo) => repo.structuresTemplate());

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

  /// Message for an import call; row errors of a refused commit are listed.
  String _errorText(ApiException e) {
    if (e.code == 'IMPORT_DUPLICATE') return 'This exact file was already imported.';
    if (e.code == 'IMPORT_NOT_CHECKED') {
      return 'Check the file again first. A check is valid for 15 minutes.';
    }
    final base = payrollErrorMessage(e);
    final rows = [
      for (final d in e.details) StructureImportIssue.fromCommitDetail(d.field, d.message),
    ].where((r) => r.row > 0).take(5).map(_issueLine);
    return rows.isEmpty ? base : '$base\n${rows.join('\n')}';
  }

  String _issueLine(StructureImportIssue r) =>
      '${r.sheet.isEmpty ? '' : '${r.sheet} '}row ${r.row}: ${structureImportErrorLabel(r.message)}';

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
      final r = await repo.structureImportDryRun(bytes: f.bytes, filename: f.name);
      if (mounted) setState(() => _report = r);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = _errorText(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _commit() async {
    final f = _file;
    final r = _report;
    if (f == null || r == null || !r.canCommit || _busy) return;
    if (!(_reasonKey.currentState?.validate() ?? false)) return;
    if (f.sha256Hex != r.fileSha256) {
      setState(
        () => _error = payrollErrorMessage(const ApiException(code: 'IMPORT_FILE_CHANGED', message: '')),
      );
      return;
    }
    final repo = ref.read(payrollRepositoryProvider);
    final reason = _reason.text.trim();
    StructureImportCommit? out;
    try {
      final ok = await runWithReauth(
        context,
        reason: 'Importing salary structures for ${r.valid} employees needs your password.',
        action: (pw) async {
          out = await repo.structureImportCommit(
            bytes: f.bytes,
            filename: f.name,
            fileSha256: r.fileSha256,
            reason: reason,
            currentPassword: pw,
          );
        },
      );
      if (!ok || !mounted) return;
      setState(() {
        _done = out;
        _error = null;
      });
      ref.invalidate(payrollEmployeesProvider);
      ref.invalidate(incrementBatchesProvider);
      ref.invalidate(approvalQueueProvider);
      ref.invalidate(incrementsProvider);
      if ((out?.arrearsProposed ?? 0) > 0) ref.invalidate(arrearsProvider);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = _errorText(e));
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final r = _report;
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Import salary structures')),
        body: ListView(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 48),
          children: [
            SectionCard(
              title: '1. Template',
              children: [
                const Text(
                  'Sheet Structures: one row per employee with a gross, or with basic, HRA and flags. '
                  'Sheet Components: allowances and fixed deductions. Delete the example rows first. '
                  'Up to 500 employees and 1 MB.',
                ),
                const SizedBox(height: 8),
                OutlinedButton.icon(
                  key: const ValueKey('simport-template'),
                  onPressed: _template,
                  icon: const Icon(Icons.download_outlined),
                  label: const Text('Download template'),
                ),
              ],
            ),
            SectionCard(
              title: '2. Choose the file',
              children: [
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: [
                    OutlinedButton.icon(
                      key: const ValueKey('simport-pick'),
                      onPressed: _busy || _done != null ? null : _pick,
                      icon: const Icon(Icons.attach_file),
                      label: Text(_file == null ? 'Choose .xlsx file' : 'Choose another file'),
                    ),
                    if (_file != null) Text(_file!.name, key: const ValueKey('simport-filename')),
                  ],
                ),
                const SizedBox(height: 12),
                FilledButton.icon(
                  key: const ValueKey('simport-check'),
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
                key: const ValueKey('simport-error'),
                color: theme.colorScheme.errorContainer,
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Text(_error!, style: TextStyle(color: theme.colorScheme.onErrorContainer)),
                ),
              ),
            if (r != null) _reportCard(r),
            if (_done != null)
              Card(
                key: const ValueKey('simport-done'),
                color: theme.colorScheme.primaryContainer,
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Text(
                    '${_done!.created} salary structures created. '
                    '${_done!.incrementsSubmitted} increments were submitted for approval '
                    '(see Approvals).'
                    '${_done!.arrearsProposed > 0 ? ' ${_done!.arrearsProposed} arrears proposals were made: review them in Pay items.' : ''}',
                    style: TextStyle(color: theme.colorScheme.onPrimaryContainer),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }

  Widget _reportCard(StructureImportReport r) {
    final theme = Theme.of(context);
    final errors = r.errors.take(_maxIssueRows).toList();
    final warnings = r.warnings.take(_maxIssueRows).toList();
    return SectionCard(
      title: '3. Check result',
      children: [
        Text(
          '${r.rows} rows, ${r.valid} valid, ${r.errors.length} with errors',
          key: const ValueKey('simport-summary'),
          style: theme.textTheme.titleSmall,
        ),
        const SizedBox(height: 4),
        Text(
          '${r.createCount} new structures, ${r.incrementCount} become increments that need approval',
          key: const ValueKey('simport-actions'),
        ),
        Text('Total monthly gross ${formatBigRupees(r.totalGross)}'),
        if (errors.isNotEmpty) ...[
          const Divider(),
          Text('Fix these rows in the file, then choose it again:', style: theme.textTheme.bodySmall),
          for (final e in errors)
            ListTile(
              key: ValueKey('simport-err-${e.sheet}-${e.row}-${e.message}'),
              dense: true,
              contentPadding: EdgeInsets.zero,
              leading: Icon(Icons.error_outline, color: theme.colorScheme.error),
              title: Text('${e.sheet} row ${e.row}: ${structureImportErrorLabel(e.message)}'),
              subtitle: e.field.isEmpty ? null : Text('Column: ${e.field}'),
            ),
          if (r.errors.length > errors.length) Text('and ${r.errors.length - errors.length} more.'),
        ],
        if (warnings.isNotEmpty)
          ExpansionTile(
            key: const ValueKey('simport-warnings'),
            tilePadding: EdgeInsets.zero,
            title: Text('${r.warnings.length} warnings (the file can still be imported)'),
            children: [
              for (final w in warnings)
                ListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  leading: const Icon(Icons.warning_amber_outlined),
                  title: Text('${w.sheet} row ${w.row}: ${structureImportWarningLabel(w.message)}'),
                ),
              if (r.warnings.length > warnings.length) Text('and ${r.warnings.length - warnings.length} more.'),
            ],
          ),
        if (r.canCommit && _done == null) ...[
          const SizedBox(height: 12),
          TextFormField(
            key: _reasonKey,
            controller: _reason,
            enabled: !_busy,
            maxLength: 200,
            maxLines: 2,
            minLines: 1,
            decoration: const InputDecoration(
              labelText: 'Reason for this import',
              border: OutlineInputBorder(),
            ),
            validator: (v) => validateComment(v, required: true)?.replaceFirst('A comment', 'A reason'),
          ),
        ],
        const SizedBox(height: 8),
        FilledButton.icon(
          key: const ValueKey('simport-commit'),
          onPressed: r.canCommit && _done == null && !_busy ? _commit : null,
          icon: const Icon(Icons.lock_outline),
          label: Text(r.canCommit ? 'Import ${r.valid} employees' : 'Cannot import yet'),
        ),
        if (r.errors.isEmpty && r.valid == 0)
          const Padding(padding: EdgeInsets.only(top: 6), child: Text('The file has no rows to import.')),
      ],
    );
  }
}
