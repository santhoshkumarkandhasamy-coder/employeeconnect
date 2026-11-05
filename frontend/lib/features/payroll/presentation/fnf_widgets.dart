import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../admin/presentation/admin_widgets.dart' show runWithReauth;
import '../data/payroll_repository.dart';
import '../domain/payroll_models.dart' show periodTitle;
import '../domain/phase4b_models.dart';
import 'increment_widgets.dart' show showCommentDialog;
import 'pay_items_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// Shared pieces of the full and final settlement screens (contract 18.8).

PayTone fnfTone(String status) => switch (status) {
      'approved' || 'settled' => PayTone.good,
      'submitted' => PayTone.warn,
      'cancelled' => PayTone.bad,
      _ => PayTone.neutral,
    };

class FnfStatusChip extends StatelessWidget {
  const FnfStatusChip(this.status, {super.key});
  final String status;

  @override
  Widget build(BuildContext context) => PayStatusChip(fnfStatusLabel(status), tone: fnfTone(status));
}

/// Checklist chips (ARREARS_PROPOSED, CLAIMS_OPEN, ...).
class FnfWarnings extends StatelessWidget {
  const FnfWarnings(this.warnings, {super.key});
  final List<String> warnings;

  @override
  Widget build(BuildContext context) {
    if (warnings.isEmpty) return const SizedBox.shrink();
    final cs = Theme.of(context).colorScheme;
    return Wrap(
      key: const ValueKey('fnf-warnings'),
      spacing: 6,
      runSpacing: 4,
      children: [
        for (final w in warnings)
          Chip(
            avatar: Icon(
              w == 'NEGATIVE_NET_ESTIMATE' ? Icons.error_outline : Icons.warning_amber_outlined,
              size: 16,
              color: w == 'NEGATIVE_NET_ESTIMATE' ? cs.onErrorContainer : null,
            ),
            label: Text(fnfWarningLabel(w)),
            backgroundColor: w == 'NEGATIVE_NET_ESTIMATE' ? cs.errorContainer : null,
            visualDensity: VisualDensity.compact,
          ),
      ],
    );
  }
}

String lastDayLabel(String ymdText) {
  final d = parseYmd(ymdText);
  return d == null ? ymdText : formatDayLong(d);
}

/// One row of the F&F list (no money).
class FnfTile extends StatelessWidget {
  const FnfTile({required this.item, required this.onTap, super.key});
  final FnfListItem item;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      key: ValueKey('fnf-${item.id}'),
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
                  Expanded(child: Text(item.user.display, style: theme.textTheme.titleSmall, maxLines: 2)),
                  FnfStatusChip(item.status),
                ],
              ),
              Text(
                '${exitReasonLabel(item.exitReason)}  -  last day ${lastDayLabel(item.lastWorkingDay)}',
                style: theme.textTheme.bodySmall,
              ),
              Text(
                'Settled in ${periodTitle(item.settlementPeriod)}  -  ${fnfRunStatusLabel(item.runStatus)}',
                style: theme.textTheme.bodySmall,
              ),
              if (item.warnings.isNotEmpty) ...[
                const SizedBox(height: 4),
                FnfWarnings(item.warnings),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

void invalidateFnfState(WidgetRef ref, {String? id}) {
  ref
    ..invalidate(fnfListProvider)
    ..invalidate(approvalQueueProvider)
    ..invalidate(payrollDashboardProvider)
    ..invalidate(payrollDetailsProvider);
  if (id != null) {
    ref
      ..invalidate(fnfProvider(id))
      ..invalidate(fnfPreviewProvider(id));
  }
}

/// Approve: optional comment, then the password (step-up). Returns the
/// approved settlement, or null when cancelled or refused (reason shown).
Future<Fnf?> approveFnfFlow(BuildContext context, WidgetRef ref, Fnf f) async {
  final comment = await showCommentDialog(
    context,
    title: 'Approve this final settlement?',
    label: 'Comment (optional)',
    confirmLabel: 'Continue',
  );
  if (comment == null || !context.mounted) return null;
  final repo = ref.read(payrollRepositoryProvider);
  Fnf? out;
  try {
    final done = await runWithReauth(
      context,
      reason: 'Approving the final settlement of ${f.user.fullName} needs your password.',
      action: (pw) async {
        out = await repo.approveFnf(f.id, expectedVersion: f.version, currentPassword: pw, comment: comment);
      },
    );
    if (!done) return null;
  } on ApiException catch (e) {
    if (isStaleFnfError(e)) invalidateFnfState(ref, id: f.id);
    if (context.mounted) payrollSnack(context, fnfErrorMessage(e));
    return null;
  }
  invalidateFnfState(ref, id: f.id);
  if (context.mounted) {
    payrollSnack(
      context,
      'Approved. The settlement lines were created for ${periodTitle(f.settlementPeriod)}. '
      'Process the payroll run of that month and publish it to settle.',
    );
  }
  return out;
}

/// Reject: a comment is required. Returns the settlement (back to draft) or null.
Future<Fnf?> rejectFnfFlow(BuildContext context, WidgetRef ref, Fnf f) async {
  final comment = await showCommentDialog(
    context,
    title: 'Reject this final settlement?',
    label: 'Reason for rejecting',
    confirmLabel: 'Reject',
    required: true,
  );
  if (comment == null || !context.mounted) return null;
  final repo = ref.read(payrollRepositoryProvider);
  try {
    final r = await repo.rejectFnf(f.id, expectedVersion: f.version, comment: comment);
    invalidateFnfState(ref, id: f.id);
    if (context.mounted) payrollSnack(context, 'Rejected. It is a draft again.');
    return r;
  } on ApiException catch (e) {
    if (isStaleFnfError(e)) invalidateFnfState(ref, id: f.id);
    if (context.mounted) payrollSnack(context, fnfErrorMessage(e));
    return null;
  }
}

/// Revoke an approved settlement: reason, then the password. Back to draft.
Future<Fnf?> revokeFnfFlow(BuildContext context, WidgetRef ref, Fnf f) async {
  final reason = await showCommentDialog(
    context,
    title: 'Revoke this approval?',
    label: 'Reason (3 to 200 characters)',
    confirmLabel: 'Continue',
    required: true,
  );
  if (reason == null || !context.mounted) return null;
  final repo = ref.read(payrollRepositoryProvider);
  Fnf? out;
  try {
    final done = await runWithReauth(
      context,
      reason: 'Revoking the final settlement of ${f.user.fullName} needs your password.',
      action: (pw) async {
        out = await repo.revokeFnf(f.id, expectedVersion: f.version, reason: reason, currentPassword: pw);
      },
    );
    if (!done) return null;
  } on ApiException catch (e) {
    if (isStaleFnfError(e)) invalidateFnfState(ref, id: f.id);
    if (context.mounted) payrollSnack(context, fnfErrorMessage(e));
    return null;
  }
  invalidateFnfState(ref, id: f.id);
  ref
    ..invalidate(oneTimeItemsProvider)
    ..invalidate(encashmentsProvider)
    ..invalidate(loansProvider);
  if (context.mounted) {
    final outside = f.loans.any((l) => l.mode == 'outside');
    payrollSnack(
      context,
      'Revoked. It is a draft again and its pay items were cancelled.'
      '${outside ? ' Loans recorded as paid outside payroll stay closed.' : ''}',
    );
  }
  return out;
}

// ---- exit dates form ------------------------------------------------------------------------

/// Resignation date, last working day, reason, notice days and settlement
/// month. Reports every change through [onChanged]; field errors come from
/// [errors] (see [validateExit]).
class ExitFields extends StatefulWidget {
  const ExitFields({
    required this.initial,
    required this.today,
    required this.onChanged,
    super.key,
    this.errors = const {},
    this.enabled = true,
  });
  final ExitValues initial;
  final DateTime today;
  final ValueChanged<ExitValues> onChanged;
  final Map<String, String> errors;
  final bool enabled;

  @override
  State<ExitFields> createState() => _ExitFieldsState();
}

class _ExitFieldsState extends State<ExitFields> {
  late ExitValues _v = widget.initial;
  late final _notice = TextEditingController(text: widget.initial.noticePeriodDays?.toString() ?? '');

  @override
  void dispose() {
    _notice.dispose();
    super.dispose();
  }

  void _set({
    DateTime? Function()? lwd,
    DateTime? Function()? resignation,
    String? reason,
    String? Function()? period,
    int? Function()? notice,
  }) {
    final newLwd = lwd == null ? _v.lastWorkingDay : lwd();
    var newPeriod = period == null ? _v.settlementPeriod : period();
    // Follow the last working day while the month is still the default one.
    if (lwd != null && newLwd != null && (_v.settlementPeriod == null || _v.lastWorkingDay == null ||
        _v.settlementPeriod == monthKey(_v.lastWorkingDay!))) {
      newPeriod = monthKey(newLwd);
    }
    setState(() {
      _v = ExitValues(
        lastWorkingDay: newLwd,
        resignationDate: resignation == null ? _v.resignationDate : resignation(),
        exitReason: reason ?? _v.exitReason,
        settlementPeriod: newPeriod,
        noticePeriodDays: notice == null ? _v.noticePeriodDays : notice(),
      );
    });
    widget.onChanged(_v);
  }

  Future<DateTime?> _pick(DateTime? current, {required DateTime first, required DateTime last}) {
    final init = current ?? widget.today;
    return showDatePicker(
      context: context,
      initialDate: init.isBefore(first) ? first : (init.isAfter(last) ? last : init),
      firstDate: first,
      lastDate: last,
    );
  }

  Widget _dateField(String key, String label, DateTime? value, VoidCallback onTap, {String? error, VoidCallback? onClear}) =>
      InkWell(
        key: ValueKey(key),
        onTap: widget.enabled ? onTap : null,
        child: InputDecorator(
          decoration: InputDecoration(
            labelText: label,
            border: const OutlineInputBorder(),
            errorText: error,
            suffixIcon: onClear != null && value != null && widget.enabled
                ? IconButton(icon: const Icon(Icons.clear), tooltip: 'Clear', onPressed: onClear)
                : const Icon(Icons.calendar_today_outlined),
          ),
          child: Text(value == null ? 'Tap to choose' : formatDayLong(value)),
        ),
      );

  @override
  Widget build(BuildContext context) {
    final t = widget.today;
    final lwd = _v.lastWorkingDay;
    final periods = lwd == null ? <String>[] : [for (var i = 0; i <= 3; i++) shiftMonth(monthKey(lwd), i)];
    const gap = SizedBox(height: 12);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _dateField(
          'exit-lwd',
          'Last working day',
          lwd,
          () async {
            final d = await _pick(lwd, first: DateTime(t.year - 1, t.month, t.day), last: DateTime(t.year, t.month, t.day + fnfMaxDaysAhead));
            if (d != null) _set(lwd: () => d);
          },
          error: widget.errors['last_working_day'],
        ),
        gap,
        _dateField(
          'exit-resignation',
          'Resignation date (optional)',
          _v.resignationDate,
          () async {
            final d = await _pick(_v.resignationDate, first: DateTime(t.year - 2, t.month, t.day), last: DateTime(t.year, t.month, t.day + fnfMaxDaysAhead));
            if (d != null) _set(resignation: () => d);
          },
          error: widget.errors['resignation_date'],
          onClear: () => _set(resignation: () => null),
        ),
        gap,
        DropdownButtonFormField<String>(
          key: const ValueKey('exit-reason'),
          initialValue: _v.exitReason,
          isExpanded: true,
          decoration: const InputDecoration(labelText: 'Reason for leaving', border: OutlineInputBorder()),
          items: [for (final r in exitReasons) DropdownMenuItem(value: r, child: Text(exitReasonLabel(r)))],
          onChanged: widget.enabled ? (v) => _set(reason: v) : null,
        ),
        gap,
        TextFormField(
          key: const ValueKey('exit-notice-days'),
          controller: _notice,
          enabled: widget.enabled,
          keyboardType: TextInputType.number,
          decoration: InputDecoration(
            labelText: 'Notice period in days (optional)',
            helperText: 'Leave empty to use the employee payroll details or the company default.',
            helperMaxLines: 2,
            border: const OutlineInputBorder(),
            errorText: widget.errors['notice_period_days'],
          ),
          onChanged: (v) => _set(notice: () => int.tryParse(v.trim())),
        ),
        gap,
        DropdownButtonFormField<String>(
          key: ValueKey('exit-period-${_v.settlementPeriod}-${periods.length}'),
          initialValue: periods.contains(_v.settlementPeriod) ? _v.settlementPeriod : null,
          isExpanded: true,
          decoration: InputDecoration(
            labelText: 'Settle in payroll of',
            border: const OutlineInputBorder(),
            errorText: widget.errors['settlement_period'],
          ),
          items: [for (final m in periods) DropdownMenuItem(value: m, child: Text(periodTitle(m)))],
          onChanged: widget.enabled ? (v) => _set(period: () => v) : null,
        ),
      ],
    );
  }
}
