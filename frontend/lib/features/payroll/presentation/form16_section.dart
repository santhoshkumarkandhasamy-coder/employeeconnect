import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../admin/presentation/admin_widgets.dart'
    show AdminPagedBody, adminPermissionsProvider, adminSelfIdProvider, runWithReauth;
import '../data/form16_picker.dart';
import '../data/payroll_repository.dart';
import '../domain/payroll_models.dart' show canManagePayroll;
import '../domain/phase4a_models.dart' show canApprovePayroll;
import '../domain/phase5b_models.dart';
import 'increment_widgets.dart' show showCommentDialog;
import 'payroll_providers.dart' show approvalQueueProvider, payrollClockProvider, payrollSettingsProvider;
import 'payroll_widgets.dart';
import 'tax_declarations_section.dart' show FyDropdown;
import 'tax_providers.dart';
import 'tax_widgets.dart';

/// Form 16 for HR (contract 19.8): list, upload with a dry run, publish and withdraw.

void invalidateForm16(WidgetRef ref) {
  ref
    ..invalidate(form16DocsProvider)
    ..invalidate(approvalQueueProvider);
}

class Form16Section extends ConsumerStatefulWidget {
  const Form16Section({super.key});

  @override
  ConsumerState<Form16Section> createState() => _Form16SectionState();
}

class _Form16SectionState extends ConsumerState<Form16Section> {
  late String _fy = fyOf(ref.read(payrollClockProvider)());
  String? _status;
  String _q = '';

  Form16Query get _query => Form16Query(fy: _fy, status: _status, q: _q);

  Future<void> _withdraw(Form16Doc d) async {
    final reason = await showCommentDialog(
      context,
      title: 'Withdraw this document?',
      label: 'Reason',
      confirmLabel: 'Continue',
      required: true,
    );
    if (reason == null || !mounted) return;
    final repo = ref.read(payrollRepositoryProvider);
    try {
      final done = await runWithReauth(
        context,
        reason: 'Withdrawing a tax document needs your password.',
        action: (pw) => repo.withdrawForm16(d.id, reason: reason, currentPassword: pw),
      );
      if (done) {
        invalidateForm16(ref);
        if (mounted) payrollSnack(context, 'Withdrawn. The employee no longer sees it.');
      }
    } on ApiException catch (e) {
      if (isStalePhase5bError(e)) invalidateForm16(ref);
      if (mounted) payrollSnack(context, phase5bErrorMessage(e));
    }
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    final manage = canManagePayroll(perms);
    final approve = canApprovePayroll(perms);
    final async = ref.watch(form16DocsProvider(_query));
    return Scaffold(
      backgroundColor: Colors.transparent,
      floatingActionButton: manage
          ? FloatingActionButton.extended(
              key: const ValueKey('form16-upload'),
              onPressed: () async {
                await Navigator.of(context).push<void>(
                  MaterialPageRoute<void>(builder: (_) => Form16UploadScreen(initialFy: _fy)),
                );
                invalidateForm16(ref);
              },
              icon: const Icon(Icons.upload_file),
              label: const Text('Upload'),
            )
          : null,
      body: AdminPagedBody<Form16Doc>(
        async: async,
        emptyMessage: 'No Form 16 documents for this selection.',
        emptyIcon: Icons.description_outlined,
        onRefresh: () async => ref.invalidate(form16DocsProvider(_query)),
        onLoadMore: () => ref.read(form16DocsProvider(_query).notifier).loadMore(),
        header: Padding(
          padding: const EdgeInsets.fromLTRB(12, 0, 12, 4),
          child: Column(
            children: [
              Row(
                children: [
                  FyDropdown(value: _fy, fieldKey: 'form16-fy', onChanged: (v) => setState(() => _fy = v)),
                  const SizedBox(width: 12),
                  Expanded(
                    child: TextField(
                      key: const ValueKey('form16-search'),
                      decoration: const InputDecoration(
                        hintText: 'Search name or employee ID',
                        prefixIcon: Icon(Icons.search),
                        isDense: true,
                      ),
                      onSubmitted: (v) => setState(() => _q = v.trim()),
                    ),
                  ),
                ],
              ),
              SingleChildScrollView(
                scrollDirection: Axis.horizontal,
                child: Row(
                  children: [
                    for (final f in <(String?, String)>[
                      (null, 'All'),
                      for (final s in form16StatusLabels.keys) (s, form16StatusLabel(s)),
                    ])
                      Padding(
                        padding: const EdgeInsets.only(right: 8),
                        child: ChoiceChip(
                          key: ValueKey('form16-filter-${f.$1 ?? 'all'}'),
                          label: Text(f.$2),
                          selected: _status == f.$1,
                          onSelected: (_) => setState(() => _status = f.$1),
                        ),
                      ),
                    if (approve)
                      ActionChip(
                        key: const ValueKey('form16-publish-open'),
                        avatar: const Icon(Icons.publish, size: 18),
                        label: const Text('Publish...'),
                        onPressed: () async {
                          await Navigator.of(context).push<void>(
                            MaterialPageRoute<void>(builder: (_) => Form16PublishScreen(fy: _fy)),
                          );
                          invalidateForm16(ref);
                        },
                      ),
                  ],
                ),
              ),
            ],
          ),
        ),
        itemBuilder: (context, d) => Card(
          key: ValueKey('form16-${d.id}'),
          margin: const EdgeInsets.symmetric(vertical: 4),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(14, 8, 6, 8),
            child: Row(
              children: [
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(d.user.display, style: Theme.of(context).textTheme.titleSmall),
                      Text('${form16KindLabel(d.kind)}  -  uploaded by ${d.uploadedBy?.display ?? '-'}'),
                      Wrap(
                        crossAxisAlignment: WrapCrossAlignment.center,
                        spacing: 8,
                        children: [
                          TaxStatusChip(form16StatusLabel(d.status), tone: form16Tone(d.status)),
                          if (d.publishedAt != null) Text('Published ${formatDayLong(d.publishedAt!)}'),
                        ],
                      ),
                    ],
                  ),
                ),
                IconButton(
                  key: ValueKey('form16-view-${d.id}'),
                  tooltip: 'Open the PDF',
                  icon: const Icon(Icons.picture_as_pdf_outlined),
                  onPressed: () => downloadPayrollFile(context, ref, (repo) => repo.form16AdminFile(d.id)),
                ),
                if (manage && d.status != 'withdrawn')
                  IconButton(
                    key: ValueKey('form16-withdraw-${d.id}'),
                    tooltip: 'Withdraw',
                    icon: const Icon(Icons.undo),
                    onPressed: () => _withdraw(d),
                  ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

// ---- upload wizard ---------------------------------------------------------------------------------

class Form16UploadScreen extends ConsumerStatefulWidget {
  const Form16UploadScreen({required this.initialFy, super.key});
  final String initialFy;

  @override
  ConsumerState<Form16UploadScreen> createState() => _Form16UploadScreenState();
}

class _Form16UploadScreenState extends ConsumerState<Form16UploadScreen> {
  late String _fy = widget.initialFy;
  String _kind = 'form16';
  List<PickedPdf> _files = const [];
  Form16DryRun? _dry;
  Form16CommitResult? _done;
  String? _error;
  bool _busy = false;

  List<PickedPdf> get _good => [for (final f in _files) if (f.ok) f];

  void _reset() {
    _dry = null;
    _done = null;
    _error = null;
  }

  Future<void> _pick() async {
    final picked = await ref.read(form16PickerProvider).pickPdfs();
    if (!mounted || picked.isEmpty) return;
    setState(() {
      _files = picked;
      _reset();
      final bad = picked.where((f) => !f.ok).firstOrNull;
      if (bad != null) _error = bad.error;
    });
  }

  List<Form16UploadFile> get _upload => [for (final f in _good) Form16UploadFile(f.name, f.bytes)];

  Future<void> _check() async {
    final err = validateForm16Selection([for (final f in _good) (name: f.name, size: f.bytes.length)]);
    if (err != null || _good.length != _files.length) {
      setState(() => _error = err ?? 'Remove the files that were refused and choose them again.');
      return;
    }
    setState(() {
      _busy = true;
      _reset();
    });
    try {
      final r = await ref.read(payrollRepositoryProvider).form16DryRun(_fy, _kind, _upload);
      if (mounted) setState(() => _dry = r);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = phase5bErrorMessage(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _commit(Form16DryRun dry) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    final repo = ref.read(payrollRepositoryProvider);
    try {
      Form16CommitResult? out;
      final ok = await runWithReauth(
        context,
        reason: 'Uploading tax documents for employees needs your password.',
        action: (pw) async {
          out = await repo.form16Commit(_fy, _kind, _upload, manifestSha256: dry.manifestSha256, currentPassword: pw);
        },
      );
      if (ok && mounted) {
        setState(() {
          _done = out;
          _dry = null;
          _files = const [];
        });
        invalidateForm16(ref);
      }
    } on ApiException catch (e) {
      // The binding of the check is single use and lasts 15 minutes: after these the check must be repeated.
      if (mounted) {
        setState(() {
          _error = phase5bErrorMessage(e);
          if (e.code == 'IMPORT_NOT_CHECKED' || e.code == 'IMPORT_FILE_CHANGED' || e.code == 'IMPORT_INVALID') _dry = null;
        });
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  String _nameOf(int index) => index >= 0 && index < _good.length ? _good[index].name : 'File ${index + 1}';

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final now = ref.read(payrollClockProvider)();
    final fys = {fyOf(now), fyOf(DateTime(now.year - 1, now.month, now.day)), widget.initialFy}.toList()
      ..sort((a, b) => b.compareTo(a));
    final dry = _dry;
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Upload Form 16')),
        body: ListView(
          padding: const EdgeInsets.fromLTRB(12, 8, 12, 48),
          children: [
            const Text(
              'Each PDF must have exactly one employee ID or PAN in its file name, for example '
              'EMP001_form16.pdf. Up to 20 files, 2 MB each. Nothing is stored until you confirm the check.',
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                Expanded(
                  child: DropdownButtonFormField<String>(
                    key: const ValueKey('form16-up-fy'),
                    initialValue: _fy,
                    decoration: const InputDecoration(labelText: 'Financial year', border: OutlineInputBorder()),
                    items: [for (final c in fys) DropdownMenuItem(value: c, child: Text(fyTitle(c)))],
                    onChanged: _busy
                        ? null
                        : (v) => setState(() {
                              _fy = v ?? _fy;
                              _reset();
                            }),
                  ),
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: DropdownButtonFormField<String>(
                    key: const ValueKey('form16-up-kind'),
                    initialValue: _kind,
                    isExpanded: true,
                    decoration: const InputDecoration(labelText: 'Document', border: OutlineInputBorder()),
                    items: [for (final k in form16Kinds) DropdownMenuItem(value: k, child: Text(form16KindLabel(k)))],
                    onChanged: _busy
                        ? null
                        : (v) => setState(() {
                              _kind = v ?? _kind;
                              _reset();
                            }),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 12),
            OutlinedButton.icon(
              key: const ValueKey('form16-pick'),
              onPressed: _busy ? null : _pick,
              icon: const Icon(Icons.attach_file),
              label: Text(_files.isEmpty ? 'Choose PDFs' : 'Choose again'),
            ),
            for (var n = 0; n < _files.length; n++)
              ListTile(
                key: ValueKey('form16-file-$n'),
                dense: true,
                contentPadding: EdgeInsets.zero,
                leading: Icon(_files[n].ok ? Icons.picture_as_pdf_outlined : Icons.error_outline),
                title: Text(_files[n].name.isEmpty ? 'Selection' : _files[n].name, maxLines: 1, overflow: TextOverflow.ellipsis),
                subtitle: _files[n].ok ? Text('${(_files[n].bytes.length / 1024).ceil()} KB') : Text(_files[n].error ?? ''),
              ),
            if (_error != null)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 8),
                child: Text(_error!, key: const ValueKey('form16-error'), style: TextStyle(color: theme.colorScheme.error)),
              ),
            if (_files.isNotEmpty && dry == null && _done == null)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: FilledButton(
                  key: const ValueKey('form16-check'),
                  onPressed: _busy ? null : _check,
                  child: _busy
                      ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                      : const Text('Check the files'),
                ),
              ),
            if (dry != null) ...[
              const SizedBox(height: 12),
              SectionCard(
                title: 'Result of the check',
                children: [
                  Text('${dry.matched.length} of ${dry.files} files matched an employee.', key: const ValueKey('form16-summary')),
                  for (final m in dry.matched)
                    ListTile(
                      key: ValueKey('form16-match-${m.index}'),
                      dense: true,
                      contentPadding: EdgeInsets.zero,
                      leading: const Icon(Icons.check_circle_outline),
                      title: Text(m.user.display),
                      subtitle: Text(
                        '${_nameOf(m.index)}${m.replaces ? '  -  replaces the document they have now' : ''}',
                      ),
                    ),
                  for (final e in dry.errors)
                    ListTile(
                      key: ValueKey('form16-problem-${e.index}'),
                      dense: true,
                      contentPadding: EdgeInsets.zero,
                      leading: Icon(Icons.error_outline, color: theme.colorScheme.error),
                      title: Text(_nameOf(e.index)),
                      subtitle: Text(form16ErrorLabel(e.message)),
                    ),
                ],
              ),
              if (dry.canCommit)
                FilledButton.icon(
                  key: const ValueKey('form16-commit'),
                  onPressed: _busy ? null : () => _commit(dry),
                  icon: const Icon(Icons.lock_outline, size: 18),
                  label: Text('Upload ${dry.matched.length} files'),
                )
              else
                const Text(
                  'Fix the files above (rename them, remove the ones that cannot be matched) and check again. '
                  'Nothing can be uploaded while a file has a problem.',
                  key: ValueKey('form16-blocked'),
                ),
            ],
            if (_done != null)
              SectionCard(
                title: 'Uploaded',
                children: [
                  Text(
                    '${_done!.created} documents uploaded'
                    '${_done!.replaced > 0 ? ', ${_done!.replaced} earlier documents replaced' : ''}. '
                    'They are not visible to employees until someone publishes them.',
                    key: const ValueKey('form16-done'),
                  ),
                ],
              ),
          ],
        ),
      ),
    );
  }
}

// ---- publish -----------------------------------------------------------------------------------------

class Form16PublishScreen extends ConsumerStatefulWidget {
  const Form16PublishScreen({required this.fy, super.key});
  final String fy;

  @override
  ConsumerState<Form16PublishScreen> createState() => _Form16PublishScreenState();
}

class _Form16PublishScreenState extends ConsumerState<Form16PublishScreen> {
  final Set<String> _picked = {};
  bool _initialised = false;
  bool _busy = false;
  String? _error;

  Form16Query get _query => Form16Query(fy: widget.fy, status: 'uploaded');

  Future<void> _publish(List<Form16Doc> docs) async {
    final chosen = [for (final d in docs) if (_picked.contains(d.id)) d];
    if (chosen.isEmpty || _busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    final repo = ref.read(payrollRepositoryProvider);
    try {
      Form16PublishResult? out;
      final ok = await runWithReauth(
        context,
        reason: 'Publishing tax documents makes them visible to employees. It needs your password.',
        action: (pw) async {
          out = await repo.publishForm16(
            widget.fy,
            [for (final d in chosen) (id: d.id, sha256: d.sha256)],
            currentPassword: pw,
          );
        },
      );
      if (!ok || !mounted) return;
      invalidateForm16(ref);
      final r = out!;
      await showDialog<void>(
        context: context,
        builder: (ctx) => AlertDialog(
          title: Text('${r.published} published'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (r.failed.isEmpty) const Text('Every document you chose is now visible to its employee.'),
                for (final f in r.failed)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 6),
                    child: Text(
                      '${chosen.where((d) => d.id == f.id).firstOrNull?.user.display ?? 'A document'}: '
                      '${form16PublishFailLabel(f.reason)}',
                      key: ValueKey('publish-fail-${f.id}'),
                    ),
                  ),
              ],
            ),
          ),
          actions: [FilledButton(onPressed: () => Navigator.pop(ctx), child: const Text('OK'))],
        ),
      );
      if (mounted) setState(_picked.clear);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = phase5bErrorMessage(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final selfId = ref.watch(adminSelfIdProvider);
    final async = ref.watch(form16DocsProvider(_query));
    final secondPerson = ref.watch(payrollSettingsProvider).valueOrNull?[form16SecondPersonKey] != false;
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: Text('Publish Form 16 ${fyTitle(widget.fy)}')),
        body: AdminPagedBody<Form16Doc>(
          async: async,
          emptyMessage: 'Nothing is waiting to be published.',
          emptyIcon: Icons.task_alt_outlined,
          onRefresh: () async => ref.invalidate(form16DocsProvider(_query)),
          onLoadMore: () => ref.read(form16DocsProvider(_query).notifier).loadMore(),
          header: Builder(
            builder: (context) {
              final docs = async.valueOrNull?.items ?? const <Form16Doc>[];
              final ok = [for (final d in docs) if (canPublishDoc(d, selfId, secondPerson: secondPerson)) d];
              if (!_initialised && ok.isNotEmpty) {
                _initialised = true;
                _picked.addAll(ok.map((d) => d.id));
              }
              return Padding(
                padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text(
                      'Employees see a document only after it is published. Open the PDFs to check them first. '
                      'You cannot publish your own document, or one you uploaded when a second person is required.',
                    ),
                    if (_error != null)
                      Text(_error!, key: const ValueKey('publish-error'), style: TextStyle(color: Theme.of(context).colorScheme.error)),
                    const SizedBox(height: 6),
                    FilledButton.icon(
                      key: const ValueKey('form16-publish'),
                      onPressed: _busy || _picked.isEmpty ? null : () => _publish(docs),
                      icon: const Icon(Icons.lock_outline, size: 18),
                      label: Text('Publish ${_picked.length} selected'),
                    ),
                  ],
                ),
              );
            },
          ),
          itemBuilder: (context, d) {
            final can = canPublishDoc(d, selfId, secondPerson: secondPerson);
            return Card(
              key: ValueKey('publish-doc-${d.id}'),
              margin: const EdgeInsets.symmetric(vertical: 4),
              child: CheckboxListTile(
                value: _picked.contains(d.id),
                onChanged: can && !_busy
                    ? (v) => setState(() => v == true ? _picked.add(d.id) : _picked.remove(d.id))
                    : null,
                title: Text(d.user.display),
                subtitle: Text(
                  '${form16KindLabel(d.kind)}  -  uploaded by ${d.uploadedBy?.display ?? '-'}'
                  '${can ? '' : '\nYou cannot publish this one.'}',
                ),
                secondary: IconButton(
                  key: ValueKey('publish-view-${d.id}'),
                  tooltip: 'Open the PDF',
                  icon: const Icon(Icons.picture_as_pdf_outlined),
                  onPressed: () => downloadPayrollFile(context, ref, (repo) => repo.form16AdminFile(d.id)),
                ),
              ),
            );
          },
        ),
      ),
    );
  }
}
