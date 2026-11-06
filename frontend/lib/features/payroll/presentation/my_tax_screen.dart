import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart' show NoPermissionScreen, adminPermissionsProvider, confirmAction;
import '../data/payroll_repository.dart';
import '../domain/claims_models.dart' show validateClaimBill;
import '../data/claim_bill_picker.dart';
import '../domain/payroll_models.dart' show canViewOwnPayslips;
import '../domain/payroll_money.dart';
import '../domain/phase4a_models.dart' show formatBigRupees;
import '../domain/phase5b_models.dart';
import '../domain/phase5c_models.dart';
import '../domain/tds_computation.dart';
import 'declaration_editor.dart';
import 'payroll_providers.dart' show payrollClockProvider;
import 'payroll_widgets.dart';
import 'tax_providers.dart';
import 'tax_widgets.dart';
import 'tds_widgets.dart';

/// "Tax" under Payslips: the signed-in employee's investment declaration, proofs and Form 16
/// (contract 19.7, 19.8). Screenshot-protected; state in memory only.
class MyTaxScreen extends ConsumerStatefulWidget {
  const MyTaxScreen({super.key});

  @override
  ConsumerState<MyTaxScreen> createState() => _MyTaxScreenState();
}

class _MyTaxScreenState extends ConsumerState<MyTaxScreen> {
  String? _fy;

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!canViewOwnPayslips(perms)) return const NoPermissionScreen(title: 'Tax');
    final now = ref.read(payrollClockProvider)();
    final fy = _fy ?? fyOf(now);
    final years = ref.watch(myTaxYearsProvider).valueOrNull ?? const <MyTaxYear>[];
    final choices = {fyOf(now), for (final y in years) y.fy}.toList()..sort((a, b) => b.compareTo(a));
    return SecureScreen(
      child: DefaultTabController(
        length: 4,
        child: Scaffold(
          appBar: AppBar(
            title: const Text('Tax'),
            actions: [
              if (choices.length > 1)
                Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: DropdownButton<String>(
                    key: const ValueKey('tax-fy'),
                    value: choices.contains(fy) ? fy : choices.first,
                    underline: const SizedBox.shrink(),
                    items: [for (final c in choices) DropdownMenuItem(value: c, child: Text(fyTitle(c)))],
                    onChanged: (v) => setState(() => _fy = v),
                  ),
                ),
            ],
            bottom: const TabBar(
              isScrollable: true,
              tabAlignment: TabAlignment.start,
              tabs: [
                Tab(key: ValueKey('tab-declaration'), text: 'Declaration'),
                Tab(key: ValueKey('tab-proofs'), text: 'Proofs'),
                Tab(key: ValueKey('tab-form16'), text: 'Form 16'),
                Tab(key: ValueKey('tab-estimate'), text: 'Estimate'),
              ],
            ),
          ),
          body: TabBarView(
            children: [
              MyDeclarationTab(fy: fy),
              MyProofsTab(fy: fy),
              const MyForm16Tab(),
              MyEstimateTab(fy: fy),
            ],
          ),
        ),
      ),
    );
  }
}

bool _notVerified(Object e) => e is ApiException && e.code == 'TAX_RULES_NOT_VERIFIED';

class MyDeclarationTab extends ConsumerWidget {
  const MyDeclarationTab({required this.fy, super.key});
  final String fy;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(myDeclarationProvider(fy));
    final repo = ref.read(payrollRepositoryProvider);
    return async.when(
      skipLoadingOnReload: true,
      loading: () => const LoadingView(label: 'Loading your declaration'),
      error: (e, _) => _notVerified(e)
          ? EmptyView(
              icon: Icons.hourglass_empty,
              message: 'Declarations for ${fyTitle(fy)} are not open yet. HR has not verified the tax rules.',
            )
          : ErrorView(message: p5bMsg(e), onRetry: () => ref.invalidate(myDeclarationProvider(fy))),
      data: (d) => RefreshIndicator(
        onRefresh: () async => ref.invalidate(myDeclarationProvider(fy)),
        child: DeclarationPane(
          declaration: d,
          sections: d.sectionsOr(fallbackSections),
          save: (body) => repo.saveMyDeclaration(body),
          submit: (f, v) => repo.submitMyDeclaration(fy: f, expectedVersion: v),
          onReload: () => ref.invalidate(myProofsProvider(fy)),
        ),
      ),
    );
  }
}

// ---- proofs -----------------------------------------------------------------------------------------

class MyProofsTab extends ConsumerWidget {
  const MyProofsTab({required this.fy, super.key});
  final String fy;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final decl = ref.watch(myDeclarationProvider(fy));
    final proofs = ref.watch(myProofsProvider(fy));
    return decl.when(
      skipLoadingOnReload: true,
      loading: () => const LoadingView(label: 'Loading proofs'),
      error: (e, _) => _notVerified(e)
          ? EmptyView(icon: Icons.hourglass_empty, message: 'Proofs for ${fyTitle(fy)} are not open yet.')
          : ErrorView(message: p5bMsg(e), onRetry: () => ref.invalidate(myDeclarationProvider(fy))),
      data: (d) => proofs.when(
        skipLoadingOnReload: true,
        loading: () => const LoadingView(label: 'Loading proofs'),
        error: (e, _) => ErrorView(message: p5bMsg(e), onRetry: () => ref.invalidate(myProofsProvider(fy))),
        data: (list) => _ProofsBody(decl: d, proofs: list, fy: fy),
      ),
    );
  }
}

class _ProofsBody extends ConsumerWidget {
  const _ProofsBody({required this.decl, required this.proofs, required this.fy});
  final TaxDeclaration decl;
  final List<TaxProof> proofs;
  final String fy;

  SectionDef _def(String code) =>
      decl.sectionsOr(fallbackSections).where((s) => s.code == code).firstOrNull ?? SectionDef(code: code, name: code);

  Future<void> _add(BuildContext context, WidgetRef ref, DeclItem item) async {
    final made = await showDialog<TaxProof>(
      context: context,
      builder: (_) => ProofDialog(
        fy: fy,
        item: item,
        sectionName: _def(item.section).name,
        maxFiles: decl.maxProofFiles,
      ),
    );
    if (made != null && context.mounted) {
      payrollSnack(context, 'Proof sent. HR will check it.');
      ref
        ..invalidate(myProofsProvider(fy))
        ..invalidate(myDeclarationProvider(fy));
    }
  }

  Future<void> _cancel(BuildContext context, WidgetRef ref, TaxProof p) async {
    final ok = await confirmAction(
      context,
      title: 'Cancel this proof?',
      message: 'HR will not check it. You can send a new proof while the proof window is open.',
      confirmLabel: 'Cancel proof',
    );
    if (!ok || !context.mounted) return;
    try {
      await ref.read(payrollRepositoryProvider).cancelMyProof(p.id);
      ref.invalidate(myProofsProvider(fy));
    } on ApiException catch (e) {
      if (isStalePhase5bError(e)) ref.invalidate(myProofsProvider(fy));
      if (context.mounted) payrollSnack(context, phase5bErrorMessage(e));
    }
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final sub = decl.submitted;
    final theme = Theme.of(context);
    if (sub == null || sub.items.isEmpty) {
      return const EmptyView(
        icon: Icons.upload_file_outlined,
        message: 'Submit your declaration first. Then add proofs for the items that need one.',
      );
    }
    final items = [for (final i in sub.items) if (_def(i.section).proofRequired) i];
    if (items.isEmpty) {
      return const EmptyView(icon: Icons.task_alt_outlined, message: 'None of your items needs a proof.');
    }
    return RefreshIndicator(
      onRefresh: () async => ref.invalidate(myProofsProvider(fy)),
      child: ListView(
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 48),
        children: [
          if (!decl.canAddProofs)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Text(
                'The proof window is closed (${decl.windows.proofText}). Ask HR to unlock your declaration to add a proof.',
                key: const ValueKey('proof-window-note'),
              ),
            ),
          for (final i in items)
            Card(
              key: ValueKey('proof-item-${i.id}'),
              child: Padding(
                padding: const EdgeInsets.all(14),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(_def(i.section).name, style: theme.textTheme.titleSmall),
                    Text('Declared ${formatRupees(i.amount)}'),
                    for (final p in proofs.where((p) => p.itemId == i.id && p.status != 'cancelled'))
                      _ProofTile(proof: p, onCancel: p.isSubmitted ? () => _cancel(context, ref, p) : null),
                    if (decl.canAddProofs && !proofs.any((p) => p.itemId == i.id && p.isLive))
                      Padding(
                        padding: const EdgeInsets.only(top: 8),
                        child: OutlinedButton.icon(
                          key: ValueKey('proof-add-${i.id}'),
                          onPressed: () => _add(context, ref, i),
                          icon: const Icon(Icons.add_a_photo_outlined),
                          label: const Text('Add proof'),
                        ),
                      ),
                  ],
                ),
              ),
            ),
        ],
      ),
    );
  }
}

class _ProofTile extends StatelessWidget {
  const _ProofTile({required this.proof, this.onCancel});
  final TaxProof proof;
  final VoidCallback? onCancel;

  @override
  Widget build(BuildContext context) {
    final p = proof;
    return Padding(
      key: ValueKey('proof-${p.id}'),
      padding: const EdgeInsets.only(top: 8),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Claimed ${formatRupees(p.claimed)}  -  ${p.files.length} file${p.files.length == 1 ? '' : 's'}'),
                if (p.verified != null) Text('Accepted ${formatRupees(p.verified)}'),
                if (p.comment != null) Text('HR comment: ${p.comment}', key: ValueKey('proof-comment-${p.id}')),
              ],
            ),
          ),
          TaxStatusChip(proofStatusLabel(p.status), tone: proofTone(p.status)),
          if (onCancel != null)
            TextButton(key: ValueKey('proof-cancel-${p.id}'), onPressed: onCancel, child: const Text('Cancel')),
        ],
      ),
    );
  }
}

class _Pick {
  _Pick(this.name, this.bytes);
  final String name;
  final List<int> bytes;
  String? fileId; // kept after a good upload so a retry does not upload twice
}

/// Claimed amount + 1..[maxFiles] files (`proof_max_files` from the server). Files are uploaded one by one
/// (`purpose=tax_proof`) and then the proof is created.
class ProofDialog extends ConsumerStatefulWidget {
  const ProofDialog({
    required this.fy,
    required this.item,
    required this.sectionName,
    super.key,
    this.maxFiles = defaultProofMaxFiles,
  });
  final int maxFiles;
  final String fy;
  final DeclItem item;
  final String sectionName;

  @override
  ConsumerState<ProofDialog> createState() => _ProofDialogState();
}

class _ProofDialogState extends ConsumerState<ProofDialog> {
  final _form = GlobalKey<FormState>();
  late final TextEditingController _claimed = TextEditingController(text: widget.item.amount);
  final List<_Pick> _picks = [];
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _claimed.dispose();
    super.dispose();
  }

  Future<void> _pick(BillSource source) async {
    if (_picks.length >= widget.maxFiles) {
      setState(() => _error = 'At most ${widget.maxFiles} files for one proof.');
      return;
    }
    final picked = await ref.read(claimBillPickerProvider).pick(source);
    if (picked == null || !mounted) return;
    final err = picked.error ??
        validateClaimBill(name: picked.name, size: picked.bytes.length) ??
        (picked.bytes.length > maxProofFileBytes ? 'The file is too large (5 MB at most).' : null);
    setState(() {
      if (err != null) {
        _error = err;
      } else {
        _error = null;
        _picks.add(_Pick(picked.name, picked.bytes));
      }
    });
  }

  Future<void> _send() async {
    if (!(_form.currentState?.validate() ?? false)) return;
    if (_picks.isEmpty) {
      setState(() => _error = 'Add at least one file (a photo or a PDF of the proof).');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    final repo = ref.read(payrollRepositoryProvider);
    try {
      for (final p in _picks) {
        p.fileId ??= await repo.uploadTaxProofFile(p.bytes, p.name);
      }
      final made = await repo.createMyProof(
        fy: widget.fy,
        itemId: widget.item.id,
        claimed: normalizeRulesMoney(_claimed.text) ?? _claimed.text.trim(),
        fileIds: [for (final p in _picks) p.fileId!],
      );
      if (mounted) Navigator.pop(context, made);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = phase5bErrorMessage(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: Text('Proof: ${widget.sectionName}', maxLines: 2, overflow: TextOverflow.ellipsis),
        content: SizedBox(
          width: double.maxFinite,
          child: SingleChildScrollView(
            child: Form(
              key: _form,
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  TextFormField(
                    key: const ValueKey('proof-claimed'),
                    controller: _claimed,
                    enabled: !_busy,
                    keyboardType: const TextInputType.numberWithOptions(decimal: true),
                    decoration: const InputDecoration(
                      labelText: 'Amount this proof covers',
                      prefixText: '₹ ',
                      border: OutlineInputBorder(),
                    ),
                    validator: (v) => validateClaimedAmount(v, widget.item.amount),
                  ),
                  const SizedBox(height: 12),
                  Wrap(
                    spacing: 8,
                    children: [
                      OutlinedButton.icon(
                        key: const ValueKey('proof-pick-camera'),
                        onPressed: _busy ? null : () => _pick(BillSource.camera),
                        icon: const Icon(Icons.photo_camera_outlined),
                        label: const Text('Camera'),
                      ),
                      OutlinedButton.icon(
                        key: const ValueKey('proof-pick-file'),
                        onPressed: _busy ? null : () => _pick(BillSource.document),
                        icon: const Icon(Icons.attach_file),
                        label: const Text('File'),
                      ),
                    ],
                  ),
                  for (var n = 0; n < _picks.length; n++)
                    ListTile(
                      key: ValueKey('proof-file-$n'),
                      dense: true,
                      contentPadding: EdgeInsets.zero,
                      leading: const Icon(Icons.insert_drive_file_outlined),
                      title: Text(_picks[n].name, maxLines: 1, overflow: TextOverflow.ellipsis),
                      subtitle: Text(_picks[n].fileId != null ? 'Uploaded' : 'Will be uploaded when you send'),
                      trailing: IconButton(
                        tooltip: 'Remove',
                        icon: const Icon(Icons.close),
                        onPressed: _busy ? null : () => setState(() => _picks.removeAt(n)),
                      ),
                    ),
                  if (_error != null)
                    Padding(
                      padding: const EdgeInsets.only(top: 8),
                      child: Text(
                        _error!,
                        key: const ValueKey('proof-error'),
                        style: TextStyle(color: Theme.of(context).colorScheme.error),
                      ),
                    ),
                ],
              ),
            ),
          ),
        ),
        actions: [
          TextButton(onPressed: _busy ? null : () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            key: const ValueKey('proof-send'),
            onPressed: _busy ? null : _send,
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: _busy
                ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('Send'),
          ),
        ],
      );
}

// ---- Form 16 -----------------------------------------------------------------------------------------

class MyForm16Tab extends ConsumerWidget {
  const MyForm16Tab({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(myForm16Provider);
    return async.when(
      skipLoadingOnReload: true,
      loading: () => const LoadingView(label: 'Loading Form 16'),
      error: (e, _) => ErrorView(message: p5bMsg(e), onRetry: () => ref.invalidate(myForm16Provider)),
      data: (docs) => RefreshIndicator(
        onRefresh: () async => ref.invalidate(myForm16Provider),
        child: docs.isEmpty
            ? ListView(
                physics: const AlwaysScrollableScrollPhysics(),
                children: const [
                  SizedBox(height: 80),
                  EmptyView(icon: Icons.description_outlined, message: 'No Form 16 has been published for you yet.'),
                ],
              )
            : ListView(
                physics: const AlwaysScrollableScrollPhysics(),
                padding: const EdgeInsets.fromLTRB(12, 8, 12, 48),
                children: [
                  for (final d in docs)
                    Card(
                      key: ValueKey('my-form16-${d.id}'),
                      child: ListTile(
                        leading: const Icon(Icons.description_outlined),
                        title: Text('${form16KindLabel(d.kind)}  -  ${fyTitle(d.fy)}'),
                        subtitle: d.publishedAt == null ? null : Text('Published ${formatDayLong(d.publishedAt!)}'),
                        trailing: IconButton(
                          key: ValueKey('my-form16-download-${d.id}'),
                          tooltip: 'Download',
                          icon: const Icon(Icons.download_outlined),
                          onPressed: () => downloadPayrollFile(context, ref, (repo) => repo.myForm16File(d.id)),
                        ),
                      ),
                    ),
                ],
              ),
      ),
    );
  }
}

// ---- tax estimate (TDS engine, 19.9) ----------------------------------------------------------------

/// The signed-in employee's tax projection for the year: an estimate from the submitted declaration, not
/// Form 16 and not a tax assessment. Engine off or rules not verified is a calm empty state.
class MyEstimateTab extends ConsumerWidget {
  const MyEstimateTab({required this.fy, super.key});
  final String fy;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(myProjectionProvider(fy));
    return async.when(
      skipLoadingOnReload: true,
      loading: () => const LoadingView(label: 'Working out your estimate'),
      error: (e, _) => tdsUnavailable(e)
          ? EmptyView(icon: Icons.calculate_outlined, message: myProjectionMessage(e))
          : ErrorView(message: myProjectionMessage(e), onRetry: () => ref.invalidate(myProjectionProvider(fy))),
      data: (r) => RefreshIndicator(
        onRefresh: () async => ref.invalidate(myProjectionProvider(fy)),
        child: _EstimateBody(result: r),
      ),
    );
  }
}

class _EstimateBody extends StatelessWidget {
  const _EstimateBody({required this.result});
  final TdsResult result;

  @override
  Widget build(BuildContext context) {
    final c = result.computation;
    final theme = Theme.of(context);
    final cmp = compareRegimes(c, result.otherRegime);
    final flags = [for (final f in c.flags) if (projectionFlags.contains(f)) f];
    return ListView(
      physics: const AlwaysScrollableScrollPhysics(),
      padding: const EdgeInsets.fromLTRB(12, 8, 12, 48),
      children: [
        Card(
          key: const ValueKey('estimate-note'),
          color: theme.colorScheme.secondaryContainer,
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Icon(Icons.info_outline, color: theme.colorScheme.onSecondaryContainer),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    'This is an estimate for ${fyTitle(c.fy)}, worked out from your salary and the '
                    'investment declaration you submitted. It is not Form 16 and not a tax assessment. '
                    'The tax actually deducted can differ.',
                    style: TextStyle(color: theme.colorScheme.onSecondaryContainer),
                  ),
                ),
              ],
            ),
          ),
        ),
        SectionCard(
          key: const ValueKey('estimate-card'),
          title: 'Your estimated tax',
          children: [
            AmountRow(
              label: 'Estimated tax for the year',
              value: formatBigRupees(c.annual.totalTax),
              bold: true,
              caption: regimeLabel(c.regime) + (c.regimeIsDefault ? ' (you have not chosen one)' : ''),
            ),
            AmountRow(label: 'Estimated TDS for this month', value: formatBigRupees(c.suggested)),
            AmountRow(label: 'Tax deducted so far', value: formatBigRupees(c.tdsPaidSoFar)),
            AmountRow(label: 'Still to be deducted', value: formatBigRupees(c.remainingTax)),
            for (final f in flags) TdsFlagTile(f, key: ValueKey('estimate-flag-$f')),
          ],
        ),
        if (cmp != null)
          SectionCard(
            title: 'Old and new regime',
            children: [
              RegimeComparisonView(comparison: cmp),
              Text(
                'You choose your regime in the declaration, while its window is open.',
                style: theme.textTheme.bodySmall,
              ),
            ],
          ),
        SectionCard(
          title: 'How it was worked out',
          children: [TdsFigures(computation: c, showSteps: false)],
        ),
      ],
    );
  }
}
