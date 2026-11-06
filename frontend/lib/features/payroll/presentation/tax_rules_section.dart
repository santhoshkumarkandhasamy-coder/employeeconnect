import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart'
    show adminPermissionsProvider, adminSelfIdProvider, confirmAction, runWithReauth;
import '../data/payroll_repository.dart';
import '../domain/payroll_models.dart' show canManagePayroll;
import '../domain/phase4a_models.dart' show canApprovePayroll;
import '../domain/phase5b_models.dart';
import 'increment_widgets.dart' show showCommentDialog;
import 'payroll_providers.dart' show approvalQueueProvider, payrollClockProvider;
import 'payroll_widgets.dart';
import 'tax_providers.dart';
import 'tax_rules_editor.dart';
import 'tax_widgets.dart';

/// Tax year rules (contract 19.6): years, versions, windows, review, submit and verify.

void invalidateTaxRules(WidgetRef ref, {String? fy, String? id}) {
  ref
    ..invalidate(taxYearsProvider)
    ..invalidate(approvalQueueProvider);
  if (fy != null) ref.invalidate(taxRuleVersionsProvider(fy));
  if (id != null) ref.invalidate(taxRulesProvider(id));
}

class TaxRulesSection extends ConsumerWidget {
  const TaxRulesSection({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(taxYearsProvider);
    return async.when(
      skipLoadingOnReload: true,
      loading: () => const LoadingView(label: 'Loading tax years'),
      error: (e, _) => ErrorView(message: p5bMsg(e), onRetry: () => ref.invalidate(taxYearsProvider)),
      data: (years) => RefreshIndicator(
        onRefresh: () async => ref.invalidate(taxYearsProvider),
        child: years.isEmpty
            ? ListView(
                physics: const AlwaysScrollableScrollPhysics(),
                children: const [
                  SizedBox(height: 80),
                  EmptyView(icon: Icons.rule_folder_outlined, message: 'No tax years yet.'),
                ],
              )
            : ListView(
                physics: const AlwaysScrollableScrollPhysics(),
                padding: const EdgeInsets.fromLTRB(12, 8, 12, 48),
                children: [
                  for (final y in years)
                    Card(
                      key: ValueKey('tax-year-${y.fy}'),
                      child: ListTile(
                        title: Text('${fyTitle(y.fy)}${y.current ? '  (this year)' : ''}'),
                        subtitle: Text(
                          y.verified ? 'Verified version ${y.verifiedVersion}' : 'No verified rules yet',
                        ),
                        trailing: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            if (y.openVersion != null)
                              TaxStatusChip(
                                'v${y.openVersion!.version} ${rulesStatusLabel(y.openVersion!.status)}',
                                tone: rulesTone(y.openVersion!.status),
                              ),
                            const Icon(Icons.chevron_right),
                          ],
                        ),
                        onTap: () async {
                          await Navigator.of(context).push<void>(
                            MaterialPageRoute<void>(builder: (_) => TaxYearScreen(fy: y.fy)),
                          );
                          ref.invalidate(taxYearsProvider);
                        },
                      ),
                    ),
                ],
              ),
      ),
    );
  }
}

class TaxYearScreen extends ConsumerStatefulWidget {
  const TaxYearScreen({required this.fy, super.key});
  final String fy;

  @override
  ConsumerState<TaxYearScreen> createState() => _TaxYearScreenState();
}

class _TaxYearScreenState extends ConsumerState<TaxYearScreen> {
  bool _busy = false;

  Future<void> _newDraft() async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      final r = await ref.read(payrollRepositoryProvider).createTaxRules(widget.fy);
      invalidateTaxRules(ref, fy: widget.fy);
      if (!mounted) return;
      payrollSnack(context, 'A new draft was made as a copy of the latest version. Review and edit it.');
      await Navigator.of(context).push<void>(MaterialPageRoute<void>(builder: (_) => TaxRulesScreen(rulesId: r.id)));
      invalidateTaxRules(ref, fy: widget.fy);
    } on ApiException catch (e) {
      if (e.code == 'DRAFT_EXISTS') invalidateTaxRules(ref, fy: widget.fy);
      if (mounted) payrollSnack(context, phase5bErrorMessage(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _windows(TaxWindows current) async {
    final saved = await showDialog<TaxWindows>(
      context: context,
      builder: (_) => WindowsDialog(fy: widget.fy, windows: current),
    );
    if (saved == null || !mounted) return;
    final repo = ref.read(payrollRepositoryProvider);
    const reason = 'Reopening a window that has already closed, or moving its closing date later, needs your password.';
    try {
      if (windowsNeedPassword(current, saved, ref.read(payrollClockProvider)())) {
        await _saveWithPassword(repo, saved, reason);
      } else {
        try {
          await repo.saveTaxWindows(widget.fy, saved);
          invalidateTaxRules(ref, fy: widget.fy);
          if (mounted) payrollSnack(context, 'Windows saved.');
        } on ApiException catch (e) {
          // The server decides in the end: it asks for the password when it sees a reopening.
          if (e.code != 'VALIDATION_ERROR' || e.detail('current_password') == null) rethrow;
          if (!mounted) return;
          await _saveWithPassword(repo, saved, reason);
        }
      }
    } on ApiException catch (e) {
      if (mounted) payrollSnack(context, phase5bErrorMessage(e));
    }
  }

  Future<void> _saveWithPassword(PayrollRepository repo, TaxWindows saved, String reason) async {
    final done = await runWithReauth(
      context,
      reason: reason,
      action: (pw) async {
        await repo.saveTaxWindows(widget.fy, saved, currentPassword: pw);
      },
    );
    if (!done || !mounted) return;
    invalidateTaxRules(ref, fy: widget.fy);
    payrollSnack(context, 'Windows saved.');
  }

  @override
  Widget build(BuildContext context) {
    final fy = widget.fy;
    final manage = canManagePayroll(ref.watch(adminPermissionsProvider));
    final versions = ref.watch(taxRuleVersionsProvider(fy));
    final years = ref.watch(taxYearsProvider).valueOrNull ?? const <TaxYear>[];
    final year = years.where((y) => y.fy == fy).firstOrNull;
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: Text('Tax rules ${fyTitle(fy)}')),
        body: versions.when(
          skipLoadingOnReload: true,
          loading: () => const LoadingView(label: 'Loading versions'),
          error: (e, _) => ErrorView(message: p5bMsg(e), onRetry: () => ref.invalidate(taxRuleVersionsProvider(fy))),
          data: (list) {
            final windows = year?.windows ?? (list.isEmpty ? const TaxWindows() : list.first.windows);
            final hasOpen = list.any((v) => v.isDraft || v.isSubmitted);
            return RefreshIndicator(
              onRefresh: () async => invalidateTaxRules(ref, fy: fy),
              child: ListView(
                physics: const AlwaysScrollableScrollPhysics(),
                padding: const EdgeInsets.fromLTRB(12, 8, 12, 48),
                children: [
                  SectionCard(
                    title: 'Declaration and proof windows',
                    children: [
                      Text('Declarations: ${windows.declarationText}', key: const ValueKey('tax-window-decl')),
                      Text('Proofs: ${windows.proofText}', key: const ValueKey('tax-window-proof')),
                      if (manage)
                        Align(
                          alignment: Alignment.centerRight,
                          child: TextButton(
                            key: const ValueKey('tax-windows-edit'),
                            onPressed: () => _windows(windows),
                            child: const Text('Change windows'),
                          ),
                        ),
                    ],
                  ),
                  if (manage && !hasOpen)
                    Align(
                      alignment: Alignment.centerLeft,
                      child: FilledButton.icon(
                        key: const ValueKey('tax-new-draft'),
                        onPressed: _busy ? null : _newDraft,
                        icon: const Icon(Icons.add),
                        label: const Text('New draft'),
                      ),
                    ),
                  const SizedBox(height: 8),
                  if (list.isEmpty)
                    const EmptyView(icon: Icons.rule_folder_outlined, message: 'No rules for this year yet.'),
                  for (final v in list)
                    Card(
                      key: ValueKey('rules-version-${v.id}'),
                      child: ListTile(
                        title: Text('Version ${v.version}'),
                        subtitle: Text(
                          v.verifiedAt != null
                              ? 'Verified by ${v.verifiedBy?.display ?? '-'} on ${formatDayLong(v.verifiedAt!)}'
                              : (v.noteIsUnverified ? 'Copied from an earlier year: not reviewed' : (v.note ?? '')),
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                        ),
                        trailing: TaxStatusChip(rulesStatusLabel(v.status), tone: rulesTone(v.status)),
                        onTap: () async {
                          await Navigator.of(context).push<void>(
                            MaterialPageRoute<void>(builder: (_) => TaxRulesScreen(rulesId: v.id)),
                          );
                          invalidateTaxRules(ref, fy: fy);
                        },
                      ),
                    ),
                ],
              ),
            );
          },
        ),
      ),
    );
  }
}

/// Opens, closes or edits the two windows. Both dates of a window or neither.
class WindowsDialog extends StatefulWidget {
  const WindowsDialog({required this.fy, required this.windows, super.key});
  final String fy;
  final TaxWindows windows;

  @override
  State<WindowsDialog> createState() => _WindowsDialogState();
}

class _WindowsDialogState extends State<WindowsDialog> {
  late final _dOpen = TextEditingController(text: widget.windows.declarationOpensOn ?? '');
  late final _dClose = TextEditingController(text: widget.windows.declarationClosesOn ?? '');
  late final _pOpen = TextEditingController(text: widget.windows.proofOpensOn ?? '');
  late final _pClose = TextEditingController(text: widget.windows.proofClosesOn ?? '');
  String? _error;

  @override
  void dispose() {
    _dOpen.dispose();
    _dClose.dispose();
    _pOpen.dispose();
    _pClose.dispose();
    super.dispose();
  }

  String? _v(TextEditingController c) => c.text.trim().isEmpty ? null : c.text.trim();

  void _save() {
    final w = TaxWindows(
      declarationOpensOn: _v(_dOpen),
      declarationClosesOn: _v(_dClose),
      proofOpensOn: _v(_pOpen),
      proofClosesOn: _v(_pClose),
    );
    final err = validateWindows(w, widget.fy);
    if (err != null) {
      setState(() => _error = err);
      return;
    }
    Navigator.pop(context, w);
  }

  Widget _f(String key, String label, TextEditingController c) => Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: TextField(
          key: ValueKey(key),
          controller: c,
          keyboardType: TextInputType.datetime,
          decoration: InputDecoration(labelText: label, hintText: 'YYYY-MM-DD', border: const OutlineInputBorder()),
        ),
      );

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: Text('Windows ${fyTitle(widget.fy)}'),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text('Leave both dates of a window empty to close it.'),
              const SizedBox(height: 12),
              _f('win-decl-open', 'Declarations open', _dOpen),
              _f('win-decl-close', 'Declarations close', _dClose),
              _f('win-proof-open', 'Proofs open', _pOpen),
              _f('win-proof-close', 'Proofs close', _pClose),
              if (_error != null)
                Text(_error!, key: const ValueKey('win-error'), style: TextStyle(color: Theme.of(context).colorScheme.error)),
            ],
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            key: const ValueKey('win-save'),
            onPressed: _save,
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: const Text('Save'),
          ),
        ],
      );
}

// ---- one version ---------------------------------------------------------------------------------

class TaxRulesScreen extends ConsumerStatefulWidget {
  const TaxRulesScreen({required this.rulesId, super.key});
  final String rulesId;

  @override
  ConsumerState<TaxRulesScreen> createState() => _TaxRulesScreenState();
}

class _TaxRulesScreenState extends ConsumerState<TaxRulesScreen> {
  bool _busy = false;

  Future<void> _run(TaxRules r, Future<void> Function(PayrollRepository repo) call, String done) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await call(ref.read(payrollRepositoryProvider));
      invalidateTaxRules(ref, fy: r.fy, id: r.id);
      if (mounted) payrollSnack(context, done);
    } on ApiException catch (e) {
      if (isStalePhase5bError(e) || e.code == 'RULES_CHANGED') invalidateTaxRules(ref, fy: r.fy, id: r.id);
      if (mounted) payrollSnack(context, phase5bErrorMessage(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _verify(TaxRules r) async {
    final attestation = await showDialog<String>(context: context, builder: (_) => AttestationDialog(rules: r));
    if (attestation == null || !mounted) return;
    if (_busy) return;
    setState(() => _busy = true);
    final repo = ref.read(payrollRepositoryProvider);
    try {
      final done = await runWithReauth(
        context,
        reason: 'Verifying tax rules changes how tax is worked out. It needs your password.',
        action: (pw) async {
          await repo.verifyTaxRules(
            r.id,
            versionRow: r.versionRow,
            rulesSha256: r.rulesSha256,
            attestation: attestation,
            currentPassword: pw,
          );
        },
      );
      if (done) {
        invalidateTaxRules(ref, fy: r.fy, id: r.id);
        if (mounted) payrollSnack(context, 'Verified. New payroll runs use these rules after a refresh.');
      }
    } on ApiException catch (e) {
      if (isStalePhase5bError(e)) invalidateTaxRules(ref, fy: r.fy, id: r.id);
      if (mounted) payrollSnack(context, phase5bErrorMessage(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _reject(TaxRules r) async {
    final comment = await showCommentDialog(
      context,
      title: 'Send these rules back?',
      label: 'What must change',
      confirmLabel: 'Reject',
      required: true,
    );
    if (comment == null || !mounted) return;
    await _run(
      r,
      (repo) async {
        await repo.rejectTaxRules(r.id, versionRow: r.versionRow, comment: comment);
      },
      'Sent back as a draft.',
    );
  }

  Future<void> _edit(TaxRules r) async {
    await Navigator.of(context).push<void>(
      MaterialPageRoute<void>(builder: (_) => TaxRulesEditorScreen(rules: r)),
    );
    invalidateTaxRules(ref, fy: r.fy, id: r.id);
  }

  Future<void> _withdraw(TaxRules r) async {
    final ok = await confirmAction(
      context,
      title: 'Withdraw these rules?',
      message: 'They go back to draft so you can edit them again.',
      confirmLabel: 'Withdraw',
    );
    if (!ok || !mounted) return;
    await _run(
      r,
      (repo) async {
        await repo.withdrawTaxRules(r.id, versionRow: r.versionRow);
      },
      'Withdrawn. You can edit the draft again.',
    );
  }

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(taxRulesProvider(widget.rulesId));
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Tax rules')),
        body: async.when(
          skipLoadingOnReload: true,
          loading: () => const LoadingView(label: 'Loading rules'),
          error: (e, _) => ErrorView(message: p5bMsg(e), onRetry: () => ref.invalidate(taxRulesProvider(widget.rulesId))),
          data: _body,
        ),
      ),
    );
  }

  Widget _body(TaxRules r) {
    final perms = ref.watch(adminPermissionsProvider);
    final selfId = ref.watch(adminSelfIdProvider);
    final manage = canManagePayroll(perms);
    final approve = canApprovePayroll(perms);
    final theme = Theme.of(context);
    final versions = ref.watch(taxRuleVersionsProvider(r.fy)).valueOrNull ?? const <TaxRules>[];
    final earlier = versions.where((v) => v.version < r.version).toList()..sort((a, b) => b.version.compareTo(a.version));
    final changes = earlier.isEmpty ? <RuleChange>[] : diffRules(earlier.first.rules, r.rules);
    final canDecide = approve && canDecideRules(r, selfId);
    final why = approve ? cannotDecideRulesReason(r, selfId) : null;
    final flat = flattenJson(r.rules);
    final groups = <String, List<MapEntry<String, String>>>{};
    for (final e in flat.entries) {
      final top = e.key.split(RegExp(r'[.\[]')).first;
      groups.putIfAbsent(top, () => []).add(e);
    }
    return RefreshIndicator(
      onRefresh: () async => invalidateTaxRules(ref, fy: r.fy, id: r.id),
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 48),
        children: [
          Card(
            child: Padding(
              padding: const EdgeInsets.all(14),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Expanded(child: Text('${fyTitle(r.fy)}  -  version ${r.version}', style: theme.textTheme.titleMedium)),
                      TaxStatusChip(rulesStatusLabel(r.status), tone: rulesTone(r.status)),
                    ],
                  ),
                  if (r.note != null) ...[
                    const SizedBox(height: 6),
                    Text(r.note!, key: const ValueKey('rules-note')),
                  ],
                  if (r.noteIsUnverified && r.isDraft)
                    Padding(
                      padding: const EdgeInsets.only(top: 6),
                      child: Text(
                        'These values were copied and have not been reviewed. Check them against the law, then change '
                        'the note (it must not start with UNVERIFIED) before you submit.',
                        key: const ValueKey('rules-unverified-warning'),
                        style: TextStyle(color: theme.colorScheme.error),
                      ),
                    ),
                  const SizedBox(height: 8),
                  if (r.createdBy != null) Text('Made by ${r.createdBy!.display}'),
                  if (r.lastEditedBy != null) Text('Last edited by ${r.lastEditedBy!.display}'),
                  if (r.submittedBy != null) Text('Submitted by ${r.submittedBy!.display}'),
                  if (r.verifiedBy != null) Text('Verified by ${r.verifiedBy!.display}'),
                  if (r.attestation != null) Text('Attestation: ${r.attestation}', key: const ValueKey('rules-attestation')),
                  const SizedBox(height: 6),
                  SelectableText('Fingerprint ${r.rulesSha256}', style: theme.textTheme.bodySmall),
                ],
              ),
            ),
          ),
          if (why != null)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 8),
              child: Text(why, key: const ValueKey('rules-cannot-decide')),
            ),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              if (manage && r.isDraft)
                OutlinedButton.icon(
                  key: const ValueKey('rules-edit'),
                  onPressed: _busy ? null : () => _edit(r),
                  icon: const Icon(Icons.edit_outlined),
                  label: const Text('Edit'),
                ),
              if (manage && r.isDraft)
                FilledButton(
                  key: const ValueKey('rules-submit'),
                  onPressed: _busy
                      ? null
                      : r.noteIsUnverified
                          ? () => payrollSnack(
                                context,
                                'Edit the note first: it still starts with UNVERIFIED, so these rules cannot be submitted.',
                              )
                          : () => _run(
                                r,
                                (repo) async {
                                  await repo.submitTaxRules(r.id, versionRow: r.versionRow);
                                },
                                'Submitted. A different person must verify them.',
                              ),
                  child: const Text('Submit for verification'),
                ),
              if (manage && r.isSubmitted)
                TextButton(
                  key: const ValueKey('rules-withdraw'),
                  onPressed: _busy ? null : () => _withdraw(r),
                  child: const Text('Withdraw'),
                ),
              if (canDecide)
                FilledButton.icon(
                  key: const ValueKey('rules-verify'),
                  onPressed: _busy ? null : () => _verify(r),
                  icon: const Icon(Icons.lock_outline, size: 18),
                  label: const Text('Verify'),
                ),
              if (canDecide)
                OutlinedButton(
                  key: const ValueKey('rules-reject'),
                  onPressed: _busy ? null : () => _reject(r),
                  child: const Text('Reject'),
                ),
            ],
          ),
          const SizedBox(height: 8),
          if (earlier.isNotEmpty)
            SectionCard(
              title: 'Changes from version ${earlier.first.version}',
              children: [
                if (changes.isEmpty)
                  const Text('No differences.', key: ValueKey('rules-diff-empty'))
                else
                  for (final c in changes.take(60))
                    Padding(
                      padding: const EdgeInsets.symmetric(vertical: 2),
                      child: Text('${c.path}: ${c.before ?? 'new'} -> ${c.after ?? 'removed'}',
                          style: theme.textTheme.bodySmall,),
                    ),
                if (changes.length > 60) Text('... and ${changes.length - 60} more'),
              ],
            ),
          SectionCard(
            title: 'All values',
            children: [
              for (final g in groups.entries)
                ExpansionTile(
                  key: ValueKey('rules-group-${g.key}'),
                  tilePadding: EdgeInsets.zero,
                  title: Text(g.key),
                  children: [
                    for (final e in g.value)
                      Align(
                        alignment: Alignment.centerLeft,
                        child: Padding(
                          padding: const EdgeInsets.only(bottom: 3),
                          child: Text('${e.key}: ${e.value}', style: theme.textTheme.bodySmall),
                        ),
                      ),
                  ],
                ),
            ],
          ),
        ],
      ),
    );
  }
}

/// Attestation text for a verify (20 to 500 characters), shown with the fingerprint being verified.
class AttestationDialog extends StatefulWidget {
  const AttestationDialog({required this.rules, super.key});
  final TaxRules rules;

  @override
  State<AttestationDialog> createState() => _AttestationDialogState();
}

class _AttestationDialogState extends State<AttestationDialog> {
  final _form = GlobalKey<FormState>();
  final _text = TextEditingController();

  @override
  void dispose() {
    _text.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: const Text('Verify these rules'),
        content: SingleChildScrollView(
          child: Form(
            key: _form,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('You confirm that you checked ${fyTitle(widget.rules.fy)} version ${widget.rules.version} '
                    'against the source.',),
                const SizedBox(height: 12),
                TextFormField(
                  key: const ValueKey('attestation-field'),
                  controller: _text,
                  maxLines: 4,
                  minLines: 2,
                  maxLength: 500,
                  decoration: const InputDecoration(
                    labelText: 'I verified these rules against ... on ...',
                    border: OutlineInputBorder(),
                  ),
                  validator: validateAttestation,
                ),
              ],
            ),
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            key: const ValueKey('attestation-confirm'),
            onPressed: () {
              if (_form.currentState?.validate() ?? false) Navigator.pop(context, _text.text.trim());
            },
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: const Text('Continue'),
          ),
        ],
      );
}
