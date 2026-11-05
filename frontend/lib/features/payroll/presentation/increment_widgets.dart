import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../admin/presentation/admin_widgets.dart' show runWithReauth;
import '../data/payroll_repository.dart';
import '../domain/pay_items_models.dart' show runStatusHint;
import '../domain/payroll_models.dart' show periodTitle;
import '../domain/payroll_money.dart';
import '../domain/payroll_models.dart' show payrollErrorMessage;
import '../domain/phase4a_models.dart';
import 'pay_items_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// Shared pieces of the increment screens (section 18.4).

PayTone incrementTone(String status) => switch (status) {
      'approved' => PayTone.good,
      'submitted' => PayTone.warn,
      'rejected' || 'cancelled' => PayTone.bad,
      _ => PayTone.neutral,
    };

class IncrementStatusChip extends StatelessWidget {
  const IncrementStatusChip(this.status, {super.key});
  final String status;

  @override
  Widget build(BuildContext context) =>
      PayStatusChip(incrementStatusLabel(status), tone: incrementTone(status));
}

/// Warning chips returned by the server (BACKDATED, DECREASE, ...).
class IncrementWarnings extends StatelessWidget {
  const IncrementWarnings(this.warnings, {super.key});
  final List<String> warnings;

  @override
  Widget build(BuildContext context) {
    if (warnings.isEmpty) return const SizedBox.shrink();
    return Wrap(
      key: const ValueKey('increment-warnings'),
      spacing: 6,
      runSpacing: 4,
      children: [
        for (final w in warnings)
          Chip(
            avatar: const Icon(Icons.warning_amber_outlined, size: 16),
            label: Text(incrementWarningLabel(w)),
            visualDensity: VisualDensity.compact,
          ),
      ],
    );
  }
}

String diffLabel(String code) => switch (code) {
      'BASIC' => 'Basic',
      'HRA' => 'HRA',
      'TDS' => 'TDS (monthly)',
      'GROSS' => 'Monthly gross',
      _ => code,
    };

/// Old -> new table of an increment (`diff[]`).
class IncrementDiffTable extends StatelessWidget {
  const IncrementDiffTable(this.diff, {super.key});
  final List<DiffLine> diff;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    if (diff.isEmpty) return const Text('No differences.');
    return Table(
      key: const ValueKey('increment-diff'),
      columnWidths: const {0: FlexColumnWidth(2), 1: FlexColumnWidth(2), 2: FlexColumnWidth(2)},
      children: [
        TableRow(children: [
          for (final h in const ['Item', 'Now', 'New'])
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 4),
              child: Text(h, style: theme.textTheme.labelMedium),
            ),
        ],),
        for (final d in diff)
          TableRow(children: [
            Padding(padding: const EdgeInsets.symmetric(vertical: 4), child: Text(diffLabel(d.code))),
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 4),
              child: Text(d.oldValue == null ? '-' : formatRupees(d.oldValue)),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 4),
              child: Text(
                d.newValue == null ? 'removed' : formatRupees(d.newValue),
                style: theme.textTheme.bodyMedium?.copyWith(fontWeight: FontWeight.w600),
              ),
            ),
          ],),
      ],
    );
  }
}

/// "+4,000.00 (10.00%)" style summary of `change`.
String changeSummary(IncrementChange c) {
  if (c.isEmpty) return 'Salary structure removed';
  final amt = formatSignedRupees(c.amount);
  final pct = c.percent == null ? '' : ' (${c.percent!.startsWith('-') ? '' : '+'}${c.percent}%)';
  return '${formatRupees(c.oldGross)} to ${formatRupees(c.newGross)}  $amt$pct';
}

String effectiveLabel(String? ymdText) {
  if (ymdText == null) return '-';
  final d = parseYmd(ymdText);
  return d == null ? ymdText : '${monthNames[d.month - 1]} ${d.year}';
}

/// One row of an increment list.
class IncrementTile extends StatelessWidget {
  const IncrementTile({required this.increment, required this.onTap, super.key, this.trailingExtra});
  final Increment increment;
  final VoidCallback onTap;
  final Widget? trailingExtra;

  @override
  Widget build(BuildContext context) {
    final i = increment;
    final theme = Theme.of(context);
    return Card(
      key: ValueKey('increment-${i.id}'),
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: InkWell(
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(14, 10, 14, 10),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(i.user.display, style: theme.textTheme.titleSmall, maxLines: 2),
                  ),
                  IncrementStatusChip(i.status),
                ],
              ),
              Text(
                '${incrementKindLabel(i.kind)}'
                '${i.effectiveFrom == null ? '' : ' from ${effectiveLabel(i.effectiveFrom)}'}'
                '${i.isInBatch ? '  -  bulk hike' : ''}',
                style: theme.textTheme.bodySmall,
              ),
              const SizedBox(height: 2),
              Text(changeSummary(i.change)),
              if (i.warnings.isNotEmpty) ...[
                const SizedBox(height: 4),
                IncrementWarnings(i.warnings),
              ],
              if (trailingExtra != null) trailingExtra!,
            ],
          ),
        ),
      ),
    );
  }
}

// ---- who may decide -------------------------------------------------------------------------

/// The checker must not be the employee, the creator or the submitter (contract
/// 18.0). Only a convenience: the server enforces it.
bool canDecideIncrement(Increment i, String? selfId) {
  if (selfId == null) return false;
  return selfId != i.user.id && selfId != i.createdBy?.id && selfId != i.submittedBy?.id;
}

/// Why [selfId] cannot decide, or null.
String? cannotDecideReason(Increment i, String? selfId) {
  if (selfId == null || canDecideIncrement(i, selfId)) return null;
  return selfId == i.user.id
      ? 'This is your own salary. You cannot decide on it.'
      : 'You made or submitted this increment. A different person must approve or reject it.';
}

// ---- dialogs ----------------------------------------------------------------------------------

/// Comment prompt. [required] = 3..200 characters; otherwise it may stay
/// empty (returns ''). Null = cancelled.
Future<String?> showCommentDialog(
  BuildContext context, {
  required String title,
  required String label,
  required String confirmLabel,
  bool required = false,
}) =>
    showDialog<String>(
      context: context,
      builder: (_) => _CommentDialog(title: title, label: label, confirmLabel: confirmLabel, required: required),
    );

class _CommentDialog extends StatefulWidget {
  const _CommentDialog({
    required this.title,
    required this.label,
    required this.confirmLabel,
    required this.required,
  });
  final String title;
  final String label;
  final String confirmLabel;
  final bool required;

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

  String? _validate(String? v) => validateComment(v, required: widget.required);

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: Text(widget.title),
        content: Form(
          key: _form,
          child: TextFormField(
            key: const ValueKey('comment-field'),
            controller: _text,
            autofocus: true,
            maxLength: 200,
            maxLines: 3,
            minLines: 1,
            decoration: InputDecoration(labelText: widget.label, border: const OutlineInputBorder()),
            validator: _validate,
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            key: const ValueKey('comment-confirm'),
            onPressed: () {
              if (_form.currentState?.validate() ?? false) Navigator.pop(context, _text.text.trim());
            },
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: Text(widget.confirmLabel),
          ),
        ],
      );
}

/// Comments and reasons: 3..200 characters (empty allowed when not required).
String? validateComment(String? v, {required bool required}) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return required ? 'A comment is required (3 to 200 characters)' : null;
  if (t.length < 3) return 'At least 3 characters';
  if (t.length > 200) return 'At most 200 characters';
  return null;
}

// ---- decision flows ----------------------------------------------------------------------------

void invalidateIncrementState(WidgetRef ref, {String? incrementId, String? userId}) {
  ref.invalidate(incrementsProvider);
  ref.invalidate(approvalQueueProvider);
  ref.invalidate(incrementBatchesProvider);
  ref.invalidate(incrementBatchProvider);
  if (incrementId != null) ref.invalidate(incrementProvider(incrementId));
  if (userId != null) {
    ref.invalidate(salaryHistoryProvider(userId));
    ref.invalidate(payrollStructuresProvider(userId));
  }
}

/// Codes after which the shown record is out of date and must be reloaded.
bool isStaleIncrementError(ApiException e) => const {
      'VERSION_CONFLICT',
      'INCREMENT_STALE',
      'NOT_SUBMITTED',
      'NOT_DRAFT',
      'STRUCTURE_EXISTS',
      'LATER_STRUCTURE_EXISTS',
      'STRUCTURE_IN_USE',
    }.contains(e.code);

/// Approve: optional comment, then the password (step-up). Returns the
/// approved increment, or null when cancelled or refused (the reason is shown).
Future<Increment?> approveIncrementFlow(BuildContext context, WidgetRef ref, Increment inc) async {
  final comment = await showCommentDialog(
    context,
    title: 'Approve this increment?',
    label: 'Comment (optional)',
    confirmLabel: 'Continue',
  );
  if (comment == null || !context.mounted) return null;
  final repo = ref.read(payrollRepositoryProvider);
  Increment? out;
  try {
    final done = await runWithReauth(
      context,
      reason: 'Approving the salary change of ${inc.user.fullName} needs your password.',
      action: (pw) async {
        out = await repo.approveIncrement(
          inc.id,
          expectedVersion: inc.version,
          currentPassword: pw,
          comment: comment,
        );
      },
    );
    if (!done) return null;
  } on ApiException catch (e) {
    if (isStaleIncrementError(e)) invalidateIncrementState(ref, incrementId: inc.id, userId: inc.user.id);
    if (context.mounted) payrollSnack(context, payrollErrorMessage(e));
    return null;
  }
  final a = out;
  invalidateIncrementState(ref, incrementId: inc.id, userId: inc.user.id);
  if (a != null && a.arrearsProposed == true) ref.invalidate(arrearsProvider);
  if (context.mounted && a != null) {
    final month = a.effectiveFrom == null ? '' : periodTitle(a.effectiveFrom!.substring(0, 7));
    final hint = runStatusHint(a.runStatus);
    payrollSnack(
      context,
      [
        'Approved. The new salary structure starts ${effectiveLabel(a.effectiveFrom)}.',
        if (a.arrearsProposed == true) 'Arrears were proposed: review them in Pay items.',
        if (a.runStatus == 'processed') 'The $month run is processed: reopen and process it again to use the new salary.',
        if (a.runStatus == 'published' && hint != null) '$hint.',
      ].join(' '),
    );
  }
  return out;
}

/// Reject: a comment is required. Returns the rejected increment or null.
Future<Increment?> rejectIncrementFlow(BuildContext context, WidgetRef ref, Increment inc) async {
  final comment = await showCommentDialog(
    context,
    title: 'Reject this increment?',
    label: 'Reason for rejecting',
    confirmLabel: 'Reject',
    required: true,
  );
  if (comment == null || !context.mounted) return null;
  final repo = ref.read(payrollRepositoryProvider);
  try {
    final r = await repo.rejectIncrement(inc.id, expectedVersion: inc.version, comment: comment);
    invalidateIncrementState(ref, incrementId: inc.id, userId: inc.user.id);
    if (context.mounted) payrollSnack(context, 'Rejected. The maker has to create a new draft to try again.');
    return r;
  } on ApiException catch (e) {
    if (isStaleIncrementError(e)) invalidateIncrementState(ref, incrementId: inc.id, userId: inc.user.id);
    if (context.mounted) payrollSnack(context, payrollErrorMessage(e));
    return null;
  }
}
