import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/utils/dates.dart';
import '../data/payroll_repository.dart';
import '../domain/claims_models.dart';
import '../domain/pay_items_models.dart' show runStatusHint;
import '../domain/payroll_money.dart';
import 'pay_items_widgets.dart';
import 'payroll_widgets.dart';

PayTone claimTone(String status) => switch (status) {
      'approved' || 'paid' => PayTone.good,
      'submitted' || 'verified' || 'processing' => PayTone.warn,
      'rejected' || 'cancelled' => PayTone.bad,
      _ => PayTone.neutral,
    };

class ClaimStatusChip extends StatelessWidget {
  const ClaimStatusChip(this.status, {super.key});
  final String status;

  @override
  Widget build(BuildContext context) => PayStatusChip(claimStatusLabel(status), tone: claimTone(status));
}

/// One claim in a list: category, amount, expense date, status. [showUser]
/// adds the employee (manager / HR lists).
class ClaimTile extends StatelessWidget {
  const ClaimTile({required this.claim, required this.onTap, super.key, this.showUser = false});
  final Claim claim;
  final VoidCallback onTap;
  final bool showUser;

  @override
  Widget build(BuildContext context) {
    final d = parseYmd(claim.expenseDate);
    return Card(
      key: ValueKey('claim-${claim.id}'),
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: ListTile(
        onTap: onTap,
        title: Text('${claim.category.label}  -  ${formatRupees(claim.amount)}'),
        subtitle: Text(
          [
            if (showUser && claim.user != null) claim.user!.display,
            if (d != null) formatDayLong(d),
            if (claim.description.isNotEmpty) claim.description,
          ].join('\n'),
          maxLines: 3,
          overflow: TextOverflow.ellipsis,
        ),
        isThreeLine: true,
        trailing: ClaimStatusChip(claim.status),
      ),
    );
  }
}

/// Detail of one claim, shared by the employee, manager and HR screens.
class ClaimDetailBody extends ConsumerWidget {
  const ClaimDetailBody({required this.claim, super.key, this.admin = false});
  final Claim claim;

  /// Admin shape: shows the employee, flags and the run hint.
  final bool admin;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final c = claim;
    final theme = Theme.of(context);
    final d = parseYmd(c.expenseDate);
    final hint = admin && c.isApproved ? runStatusHint(c.runStatus) : null;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Expanded(child: Text(c.category.label, style: theme.textTheme.titleMedium)),
            ClaimStatusChip(c.status),
          ],
        ),
        if (admin && c.user != null) Text(c.user!.display, style: theme.textTheme.bodyMedium),
        SectionCard(
          title: 'Claim',
          children: [
            AmountRow(label: 'Amount claimed', value: formatRupees(c.amount), bold: true),
            if (c.approvedAmount != null)
              AmountRow(label: 'Approved amount', value: formatRupees(c.approvedAmount), bold: true),
            if (d != null) _kv(context, 'Expense date', formatDayLong(d)),
            if (c.periodText != null) _kv(context, 'Paid with', '${c.periodText} payroll'),
            _kv(context, 'Description', c.description),
            if (hint != null) Padding(padding: const EdgeInsets.only(top: 4), child: Text(hint)),
            for (final f in c.flags)
              Padding(padding: const EdgeInsets.only(top: 4), child: Text(claimFlagLabel(f))),
          ],
        ),
        if (c.verifier != null || c.approver != null || c.decisionComment != null)
          SectionCard(
            title: 'Decision',
            children: [
              if (c.verifier != null)
                _kv(
                  context,
                  'Verified by',
                  '${c.verifier!.display}${c.verifiedAt == null ? '' : ', ${formatDayLong(c.verifiedAt!)}'}',
                ),
              if (c.approver != null)
                _kv(
                  context,
                  c.status == ClaimStatus.rejected ? 'Decided by' : 'Approved by',
                  '${c.approver!.display}${c.decidedAt == null ? '' : ', ${formatDayLong(c.decidedAt!)}'}',
                ),
              if (c.decisionComment != null) _kv(context, 'Comment', c.decisionComment!),
            ],
          ),
        if (c.bill != null) ClaimBillButton(bill: c.bill!, claimId: c.id),
      ],
    );
  }

  Widget _kv(BuildContext context, String k, String v) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(width: 110, child: Text(k, style: Theme.of(context).textTheme.bodySmall)),
            Expanded(child: Text(v)),
          ],
        ),
      );
}

/// "View bill". The bill is fetched with the bearer header into memory (never
/// a URL with a token); an image opens in a dialog, a PDF goes to the share
/// sheet / browser download and is not kept.
class ClaimBillButton extends ConsumerStatefulWidget {
  const ClaimBillButton({required this.bill, required this.claimId, super.key});
  final ClaimBill bill;
  final String claimId;

  @override
  ConsumerState<ClaimBillButton> createState() => _ClaimBillButtonState();
}

class _ClaimBillButtonState extends ConsumerState<ClaimBillButton> {
  bool _busy = false;

  Future<void> _open() async {
    if (_busy) return;
    final repo = ref.read(payrollRepositoryProvider);
    final messenger = ScaffoldMessenger.of(context);
    setState(() => _busy = true);
    try {
      final Uint8List bytes = await repo.billBytes(widget.bill.url);
      if (!mounted) return;
      setState(() => _busy = false); // the spinner is for the fetch, not for the open dialog
      if (widget.bill.isPdf) {
        await downloadPayrollFile(
          context,
          ref,
          (_) async => PayrollFile(bytes, 'claim-bill-${widget.claimId.length > 8 ? widget.claimId.substring(0, 8) : widget.claimId}.pdf'),
        );
      } else {
        await showDialog<void>(
          context: context,
          builder: (ctx) => Dialog(
            child: Stack(
              children: [
                InteractiveViewer(
                  child: Image.memory(
                    bytes,
                    fit: BoxFit.contain,
                    semanticLabel: 'Bill',
                    errorBuilder: (_, __, ___) =>
                        const Padding(padding: EdgeInsets.all(24), child: Text('The image could not be shown.')),
                  ),
                ),
                Positioned(
                  right: 0,
                  top: 0,
                  child: IconButton(
                    tooltip: 'Close',
                    icon: const Icon(Icons.close),
                    onPressed: () => Navigator.pop(ctx),
                  ),
                ),
              ],
            ),
          ),
        );
      }
    } on Object catch (e) {
      messenger
        ..hideCurrentSnackBar()
        ..showSnackBar(SnackBar(content: Text(payrollMsg(e))));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Align(
        alignment: Alignment.centerLeft,
        child: OutlinedButton.icon(
          key: const ValueKey('claim-view-bill'),
          onPressed: _busy ? null : _open,
          icon: _busy
              ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
              : const Icon(Icons.attach_file),
          label: Text(widget.bill.isPdf ? 'Open bill (PDF)' : 'View bill'),
        ),
      );
}

/// Asks for a comment. '' = none (optional comments); null = cancelled.
Future<String?> showClaimCommentDialog(
  BuildContext context, {
  required String title,
  required String label,
  required String confirmLabel,
  required bool required,
  String? message,
}) =>
    showDialog<String>(
      context: context,
      builder: (_) => _CommentDialog(
        title: title,
        label: label,
        confirmLabel: confirmLabel,
        required: required,
        message: message,
      ),
    );

class _CommentDialog extends StatefulWidget {
  const _CommentDialog({
    required this.title,
    required this.label,
    required this.confirmLabel,
    required this.required,
    this.message,
  });
  final String title;
  final String label;
  final String confirmLabel;
  final bool required;
  final String? message;

  @override
  State<_CommentDialog> createState() => _CommentDialogState();
}

class _CommentDialogState extends State<_CommentDialog> {
  final _form = GlobalKey<FormState>();
  final _text = TextEditingController();

  @override
  void dispose() {
    _text.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: Text(widget.title),
        content: Form(
          key: _form,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (widget.message != null) ...[Text(widget.message!), const SizedBox(height: 12)],
              TextFormField(
                key: const ValueKey('claim-comment'),
                controller: _text,
                autofocus: true,
                maxLength: 200,
                maxLines: 3,
                minLines: 1,
                decoration: InputDecoration(labelText: widget.label, border: const OutlineInputBorder()),
                validator: (v) => validateClaimComment(v, required: widget.required),
              ),
            ],
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            key: const ValueKey('claim-comment-ok'),
            onPressed: () {
              if (_form.currentState?.validate() ?? false) Navigator.pop(context, _text.text.trim());
            },
            child: Text(widget.confirmLabel),
          ),
        ],
      );
}
