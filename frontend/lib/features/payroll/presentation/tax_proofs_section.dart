import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../admin/presentation/admin_widgets.dart' show AdminPagedBody, adminPermissionsProvider, adminSelfIdProvider;
import '../data/payroll_repository.dart';
import '../domain/payroll_models.dart' show canManagePayroll;
import '../domain/payroll_money.dart';
import '../domain/phase5b_models.dart';
import 'payroll_providers.dart' show approvalQueueProvider, payrollClockProvider;
import 'payroll_widgets.dart';
import 'tax_declarations_section.dart' show FyDropdown;
import 'tax_providers.dart';
import 'tax_widgets.dart';

/// Proof queue for HR (contract 19.7): look at the files, accept an amount or reject with a reason.

class TaxProofsSection extends ConsumerStatefulWidget {
  const TaxProofsSection({super.key});

  @override
  ConsumerState<TaxProofsSection> createState() => _TaxProofsSectionState();
}

class _TaxProofsSectionState extends ConsumerState<TaxProofsSection> {
  late String _fy = fyOf(ref.read(payrollClockProvider)());
  String? _status = 'submitted';

  @override
  Widget build(BuildContext context) => TaxProofsList(
        query: TaxProofQuery(fy: _fy, status: _status),
        header: Padding(
          padding: const EdgeInsets.fromLTRB(12, 0, 12, 4),
          child: Row(
            children: [
              FyDropdown(value: _fy, fieldKey: 'proof-fy', onChanged: (v) => setState(() => _fy = v)),
              const SizedBox(width: 12),
              Expanded(
                child: SingleChildScrollView(
                  scrollDirection: Axis.horizontal,
                  child: Row(
                    children: [
                      for (final f in <(String?, String)>[
                        (null, 'All'),
                        for (final s in const ['submitted', 'verified', 'rejected']) (s, proofStatusLabel(s)),
                      ])
                        Padding(
                          padding: const EdgeInsets.only(right: 8),
                          child: ChoiceChip(
                            key: ValueKey('proof-filter-${f.$1 ?? 'all'}'),
                            label: Text(f.$2),
                            selected: _status == f.$1,
                            onSelected: (_) => setState(() => _status = f.$1),
                          ),
                        ),
                    ],
                  ),
                ),
              ),
            ],
          ),
        ),
      );
}

/// Proofs of one employee (opened from their declaration).
class TaxProofsScreen extends StatelessWidget {
  const TaxProofsScreen({required this.query, super.key});
  final TaxProofQuery query;

  @override
  Widget build(BuildContext context) => SecureScreen(
        child: Scaffold(
          appBar: AppBar(title: const Text('Proofs')),
          body: TaxProofsList(query: query),
        ),
      );
}

class TaxProofsList extends ConsumerWidget {
  const TaxProofsList({required this.query, super.key, this.header});
  final TaxProofQuery query;
  final Widget? header;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(taxProofsProvider(query));
    return AdminPagedBody<TaxProof>(
      async: async,
      header: header,
      emptyMessage: 'No proofs here.',
      emptyIcon: Icons.fact_check_outlined,
      onRefresh: () async => ref.invalidate(taxProofsProvider(query)),
      onLoadMore: () => ref.read(taxProofsProvider(query).notifier).loadMore(),
      itemBuilder: (context, p) => Card(
        key: ValueKey('tax-proof-${p.id}'),
        margin: const EdgeInsets.symmetric(vertical: 4),
        child: ListTile(
          title: Text(p.user?.display ?? 'Employee'),
          subtitle: Text('${p.section}  -  claimed ${formatRupees(p.claimed)}  -  ${p.files.length} files'),
          trailing: TaxStatusChip(proofStatusLabel(p.status), tone: proofTone(p.status)),
          onTap: () async {
            await Navigator.of(context).push<void>(
              MaterialPageRoute<void>(builder: (_) => ProofReviewScreen(proof: p)),
            );
            ref
              ..invalidate(taxProofsProvider(query))
              ..invalidate(approvalQueueProvider);
          },
        ),
      ),
    );
  }
}

class ProofReviewScreen extends ConsumerStatefulWidget {
  const ProofReviewScreen({required this.proof, super.key});
  final TaxProof proof;

  @override
  ConsumerState<ProofReviewScreen> createState() => _ProofReviewScreenState();
}

class _ProofReviewScreenState extends ConsumerState<ProofReviewScreen> {
  final _form = GlobalKey<FormState>();
  late final TextEditingController _verified = TextEditingController(text: widget.proof.claimed);
  final _comment = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _verified.dispose();
    _comment.dispose();
    super.dispose();
  }

  Future<void> _accept() async {
    final p = widget.proof;
    final v = validateVerifiedAmount(_verified.text, p.claimed, comment: _comment.text);
    final c = validateComment(_comment.text);
    if (v != null || c != null) {
      setState(() => _error = v ?? c);
      return;
    }
    await _decide('Proof verified.', (repo) async {
      await repo.verifyTaxProof(
        p.id,
        expectedVersion: p.version,
        verified: normalizeRulesMoney(_verified.text) ?? _verified.text.trim(),
        comment: _comment.text,
      );
    });
  }

  Future<void> _reject() async {
    final p = widget.proof;
    final c = validateComment(_comment.text, required: true);
    if (c != null) {
      setState(() => _error = 'To reject, write the reason in the comment: $c');
      return;
    }
    await _decide('Proof rejected.', (repo) async {
      await repo.rejectTaxProof(p.id, expectedVersion: p.version, comment: _comment.text.trim());
    });
  }

  Future<void> _decide(String done, Future<void> Function(PayrollRepository repo) call) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await call(ref.read(payrollRepositoryProvider));
      if (!mounted) return;
      payrollSnack(context, done);
      Navigator.pop(context);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _error = phase5bErrorMessage(e));
      if (isStalePhase5bError(e)) ref.invalidate(taxProofsProvider);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final p = widget.proof;
    final theme = Theme.of(context);
    final manage = canManagePayroll(ref.watch(adminPermissionsProvider));
    final selfId = ref.watch(adminSelfIdProvider);
    final canDecide = manage && canDecideProof(p, selfId);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Review proof')),
        body: ListView(
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
                        Expanded(child: Text(p.user?.display ?? 'Employee', style: theme.textTheme.titleMedium)),
                        TaxStatusChip(proofStatusLabel(p.status), tone: proofTone(p.status)),
                      ],
                    ),
                    Text('${p.section}  -  ${fyTitle(p.fy)}'),
                    Text('Claimed ${formatRupees(p.claimed)}', key: const ValueKey('proof-review-claimed')),
                    if (p.verified != null) Text('Accepted ${formatRupees(p.verified)}'),
                    if (p.comment != null) Text('Comment: ${p.comment}'),
                  ],
                ),
              ),
            ),
            SectionCard(
              title: 'Files',
              children: [
                if (p.files.isEmpty) const Text('No files.'),
                for (var n = 0; n < p.files.length; n++)
                  ListTile(
                    key: ValueKey('proof-open-$n'),
                    dense: true,
                    contentPadding: EdgeInsets.zero,
                    leading: Icon(p.files[n].isPdf ? Icons.picture_as_pdf_outlined : Icons.image_outlined),
                    title: Text('File ${n + 1}'),
                    trailing: const Icon(Icons.open_in_new),
                    onTap: () => downloadPayrollFile(context, ref, (repo) => repo.taxProofFile(p.files[n])),
                  ),
              ],
            ),
            if (p.isSubmitted && !canDecide)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 8),
                child: Text(
                  !manage
                      ? 'You can look at this proof but not decide on it.'
                      : 'This is your own proof. A different person must decide on it.',
                  key: const ValueKey('proof-cannot-decide'),
                ),
              ),
            if (canDecide)
              Form(
                key: _form,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const SizedBox(height: 8),
                    TextFormField(
                      key: const ValueKey('proof-verified'),
                      controller: _verified,
                      keyboardType: const TextInputType.numberWithOptions(decimal: true),
                      decoration: const InputDecoration(
                        labelText: 'Amount accepted',
                        helperText: 'Up to the amount claimed. 0 accepts nothing and needs a comment.',
                        prefixText: '₹ ',
                        border: OutlineInputBorder(),
                      ),
                    ),
                    const SizedBox(height: 10),
                    TextFormField(
                      key: const ValueKey('proof-comment'),
                      controller: _comment,
                      maxLength: 200,
                      decoration: const InputDecoration(labelText: 'Comment (needed to reject)', border: OutlineInputBorder()),
                    ),
                    if (_error != null)
                      Text(_error!, key: const ValueKey('proof-review-error'), style: TextStyle(color: theme.colorScheme.error)),
                    const SizedBox(height: 8),
                    Wrap(
                      spacing: 8,
                      runSpacing: 8,
                      children: [
                        FilledButton(
                          key: const ValueKey('proof-accept'),
                          onPressed: _busy ? null : _accept,
                          child: const Text('Verify'),
                        ),
                        OutlinedButton(
                          key: const ValueKey('proof-reject'),
                          onPressed: _busy ? null : _reject,
                          child: const Text('Reject'),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
          ],
        ),
      ),
    );
  }
}
