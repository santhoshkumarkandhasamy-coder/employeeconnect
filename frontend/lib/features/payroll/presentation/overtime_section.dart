import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../admin/presentation/admin_widgets.dart' show AdminPagedBody, adminSelfIdProvider, confirmAction;
import '../data/payroll_repository.dart';
import '../domain/claims_models.dart';
import '../domain/payroll_models.dart' show payrollErrorMessage;
import 'claims_widgets.dart';
import 'pay_items_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// Overtime review (HR / admin), shown only while `payroll.overtime_enabled`
/// is on: month, "Compute", rows grouped by employee with the month summary,
/// approve (minutes, never above the computed minutes) / reject (comment) per
/// row, and bulk approve / reject of up to 200 selected rows. No money here.
class OvertimeSection extends ConsumerStatefulWidget {
  const OvertimeSection({required this.canManage, super.key});
  final bool canManage;

  @override
  ConsumerState<OvertimeSection> createState() => _OvertimeSectionState();
}

class _OvertimeSectionState extends ConsumerState<OvertimeSection> {
  late final String _thisMonth = monthKey(ref.read(payrollClockProvider)());
  late String _period = _thisMonth;
  String? _status;
  final Set<String> _selected = {};
  bool _busy = false;
  bool _includeFlagged = false; // bulk approve also takes SUSPICIOUS / AUTO_CLOSED rows (ATTENDANCE_CHANGED never)

  OvertimeQuery get _query => OvertimeQuery(period: _period, status: _status);

  void _reload() {
    ref.invalidate(overtimeProvider);
    ref.invalidate(overtimeSummaryProvider);
    if (mounted) setState(_selected.clear);
  }

  /// Runs a call; `OVERTIME_DISABLED` also hides the segment (setting is off now).
  Future<bool> _guard(Future<bool> Function() call) async {
    try {
      return await call();
    } on ApiException catch (e) {
      if (e.code == 'OVERTIME_DISABLED') {
        ref.invalidate(payrollSettingsProvider);
        ref.invalidate(overtimeEnabledProvider);
      }
      if (mounted) payrollSnack(context, payrollErrorMessage(e));
      return false;
    }
  }

  Future<void> _compute() async {
    if (_busy) return;
    final ok = await confirmAction(
      context,
      title: 'Compute overtime?',
      message: 'Overtime is worked out from attendance for ${monthTitle(_period)}. '
          'Rows you have already reviewed are kept; they are only flagged if attendance changed.',
      confirmLabel: 'Compute',
    );
    if (!ok || !mounted) return;
    final repo = ref.read(payrollRepositoryProvider);
    setState(() => _busy = true);
    await _guard(() async {
      final r = await repo.computeOvertime(_period);
      if (mounted) {
        payrollSnack(
          context,
          'Computed: ${r.created} new, ${r.updated} updated'
          '${r.flaggedChanged > 0 ? ', ${r.flaggedChanged} flagged because attendance changed' : ''}.',
        );
      }
      return true;
    });
    _reload();
    if (mounted) setState(() => _busy = false);
  }

  Future<void> _approve(OvertimeDay d) async {
    final r = await showDialog<({int minutes, String? comment})>(
      context: context,
      builder: (_) => OvertimeApproveDialog(day: d),
    );
    if (r == null || !mounted) return;
    final repo = ref.read(payrollRepositoryProvider);
    await _guard(() async {
      await repo.approveOvertime(d.id, approvedMinutes: r.minutes, expectedVersion: d.version, comment: r.comment);
      if (mounted) payrollSnack(context, 'Overtime approved.');
      return true;
    });
    _reload(); // VERSION_CONFLICT / ITEM_LOCKED / PERIOD_LOCKED: the row changed
  }

  Future<void> _reject(OvertimeDay d) async {
    final c = await showClaimCommentDialog(
      context,
      title: 'Reject overtime?',
      label: 'Reason for rejecting',
      confirmLabel: 'Reject',
      required: true,
    );
    if (c == null || !mounted) return;
    final repo = ref.read(payrollRepositoryProvider);
    await _guard(() async {
      await repo.rejectOvertime(d.id, comment: c, expectedVersion: d.version);
      if (mounted) payrollSnack(context, 'Overtime rejected.');
      return true;
    });
    _reload();
  }

  Future<void> _bulk(String action) async {
    if (_selected.isEmpty || _busy) return;
    String? comment;
    if (action == 'reject') {
      comment = await showClaimCommentDialog(
        context,
        title: 'Reject ${_selected.length} rows?',
        label: 'Reason for rejecting',
        confirmLabel: 'Reject',
        required: true,
      );
      if (comment == null || !mounted) return;
    } else {
      final ok = await confirmAction(
        context,
        title: 'Approve ${_selected.length} rows?',
        message: 'Each row is approved with its computed minutes.',
        confirmLabel: 'Approve',
      );
      if (!ok || !mounted) return;
    }
    final repo = ref.read(payrollRepositoryProvider);
    final ids = _selected.toList();
    setState(() => _busy = true);
    await _guard(() async {
      final r = await repo.bulkOvertime(ids, action: action, comment: comment, includeFlagged: _includeFlagged);
      if (mounted) payrollSnack(context, r.message);
      return true;
    });
    _reload();
    if (mounted) setState(() => _busy = false);
  }

  void _toggle(OvertimeDay d, bool on) {
    if (on && _selected.length >= overtimeBulkMax && !_selected.contains(d.id)) {
      payrollSnack(context, 'You can select up to $overtimeBulkMax rows at once.');
      return;
    }
    setState(() => on ? _selected.add(d.id) : _selected.remove(d.id));
  }

  @override
  Widget build(BuildContext context) {
    final q = _query;
    final async = ref.watch(overtimeProvider(q));
    final summary = ref.watch(overtimeSummaryProvider(q)).valueOrNull ?? const <OvertimeSummary>[];
    final byUser = {for (final s in summary) s.userId: s};
    final selfId = ref.watch(adminSelfIdProvider);
    final loaded = async.valueOrNull?.items ?? const <OvertimeDay>[];
    return Column(
      children: [
        PayMonthBar(
          keyPrefix: 'ot',
          period: _period,
          max: _thisMonth,
          onChanged: (p) => setState(() {
            _period = p;
            _selected.clear();
          }),
        ),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12),
          child: Wrap(
            spacing: 8,
            runSpacing: 8,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              SizedBox(
                width: 150,
                child: DropdownButtonFormField<String?>(
                  key: const ValueKey('ot-status'),
                  initialValue: _status,
                  isExpanded: true,
                  decoration: const InputDecoration(
                    labelText: 'Status',
                    isDense: true,
                    border: OutlineInputBorder(),
                  ),
                  items: [
                    const DropdownMenuItem<String?>(child: Text('All')),
                    for (final s in ['computed', 'approved', 'rejected', 'processing', 'paid'])
                      DropdownMenuItem<String?>(value: s, child: Text(overtimeStatusLabel(s))),
                  ],
                  onChanged: (v) => setState(() {
                    _status = v;
                    _selected.clear();
                  }),
                ),
              ),
              if (widget.canManage)
                FilledButton.icon(
                  key: const ValueKey('ot-compute'),
                  onPressed: _busy ? null : _compute,
                  icon: const Icon(Icons.calculate_outlined),
                  label: const Text('Compute'),
                ),
              if (widget.canManage && _selected.isNotEmpty) ...[
                FilledButton.tonalIcon(
                  key: const ValueKey('ot-bulk-approve'),
                  onPressed: _busy ? null : () => _bulk('approve'),
                  icon: const Icon(Icons.check),
                  label: Text('Approve ${_selected.length}'),
                ),
                FilterChip(
                  key: const ValueKey('ot-include-flagged'),
                  label: const Text('Include flagged'),
                  selected: _includeFlagged,
                  onSelected: _busy ? null : (v) => setState(() => _includeFlagged = v),
                ),
                OutlinedButton.icon(
                  key: const ValueKey('ot-bulk-reject'),
                  onPressed: _busy ? null : () => _bulk('reject'),
                  icon: const Icon(Icons.close),
                  label: Text('Reject ${_selected.length}'),
                ),
              ],
            ],
          ),
        ),
        Expanded(
          child: AdminPagedBody<OvertimeDay>(
            async: async,
            emptyMessage: 'No overtime for ${monthTitle(_period)}. Use Compute to build it from attendance.',
            emptyIcon: Icons.more_time_outlined,
            onRefresh: () async {
              ref.invalidate(overtimeSummaryProvider(q));
              await ref.read(overtimeProvider(q).notifier).reload();
            },
            onLoadMore: () => ref.read(overtimeProvider(q).notifier).loadMore(),
            itemBuilder: (context, d) {
              final i = loaded.indexWhere((e) => e.id == d.id);
              final firstOfUser = i <= 0 || loaded[i - 1].user.id != d.user.id;
              return Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (firstOfUser) _UserHeader(user: d.user.display, summary: byUser[d.user.id]),
                  _DayRow(
                    day: d,
                    canManage: widget.canManage,
                    own: selfId != null && d.user.id == selfId,
                    selected: _selected.contains(d.id),
                    onSelect: (on) => _toggle(d, on),
                    onApprove: () => _approve(d),
                    onReject: () => _reject(d),
                  ),
                ],
              );
            },
          ),
        ),
      ],
    );
  }
}

class _UserHeader extends StatelessWidget {
  const _UserHeader({required this.user, this.summary});
  final String user;
  final OvertimeSummary? summary;

  @override
  Widget build(BuildContext context) {
    final s = summary;
    return Padding(
      padding: const EdgeInsets.fromLTRB(4, 12, 4, 2),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(user, style: Theme.of(context).textTheme.titleSmall),
          if (s != null)
            Text(
              'Approved ${minutesText(s.approvedMinutes)} of ${minutesText(s.computedMinutes)} computed'
              '${s.pendingRows > 0 ? ', ${s.pendingRows} to review' : ''}',
              style: Theme.of(context).textTheme.bodySmall,
            ),
        ],
      ),
    );
  }
}

class _DayRow extends StatelessWidget {
  const _DayRow({
    required this.day,
    required this.canManage,
    required this.own,
    required this.selected,
    required this.onSelect,
    required this.onApprove,
    required this.onReject,
  });
  final OvertimeDay day;
  final bool canManage;
  final bool own;
  final bool selected;
  final ValueChanged<bool> onSelect;
  final VoidCallback onApprove;
  final VoidCallback onReject;

  @override
  Widget build(BuildContext context) {
    final d = parseYmd(day.workDate);
    final actions = canManage && day.reviewable && !own;
    return Card(
      key: ValueKey('ot-${day.id}'),
      margin: const EdgeInsets.symmetric(vertical: 3),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(4, 6, 4, 6),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (actions)
              Checkbox(
                key: ValueKey('ot-check-${day.id}'),
                value: selected,
                onChanged: (v) => onSelect(v ?? false),
              )
            else
              const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(d == null ? day.workDate : formatDayLong(d)),
                  Text(
                    'Computed ${minutesText(day.computedMinutes)}'
                    '${day.approvedMinutes == null ? '' : ', approved ${minutesText(day.approvedMinutes!)}'}',
                  ),
                  payNotes(context, [
                    for (final f in day.flags) overtimeFlagLabel(f),
                    if (day.comment != null) 'Comment: ${day.comment}',
                  ]),
                  const SizedBox(height: 2),
                  PayStatusChip(
                    overtimeStatusLabel(day.status),
                    tone: switch (day.status) {
                      'approved' || 'paid' => PayTone.good,
                      'computed' || 'processing' => PayTone.warn,
                      'rejected' => PayTone.bad,
                      _ => PayTone.neutral,
                    },
                  ),
                ],
              ),
            ),
            if (actions)
              PopupMenuButton<String>(
                key: ValueKey('ot-menu-${day.id}'),
                tooltip: 'Actions',
                onSelected: (v) => v == 'approve' ? onApprove() : onReject(),
                itemBuilder: (_) => const [
                  PopupMenuItem(value: 'approve', child: Text('Approve')),
                  PopupMenuItem(value: 'reject', child: Text('Reject')),
                ],
              ),
          ],
        ),
      ),
    );
  }
}

/// Approve one day: whole minutes from 0 to the computed minutes.
class OvertimeApproveDialog extends StatefulWidget {
  const OvertimeApproveDialog({required this.day, super.key});
  final OvertimeDay day;

  @override
  State<OvertimeApproveDialog> createState() => _OvertimeApproveDialogState();
}

class _OvertimeApproveDialogState extends State<OvertimeApproveDialog> {
  final _form = GlobalKey<FormState>();
  late final _minutes = TextEditingController(
    text: '${widget.day.approvedMinutes ?? widget.day.computedMinutes}',
  );
  final _comment = TextEditingController();

  @override
  void dispose() {
    _minutes.dispose();
    _comment.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: const Text('Approve overtime'),
        content: SingleChildScrollView(
          child: Form(
            key: _form,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text('${widget.day.user.display}\nComputed ${minutesText(widget.day.computedMinutes)} '
                    '(${widget.day.computedMinutes} min)',),
                const SizedBox(height: 12),
                TextFormField(
                  key: const ValueKey('ot-minutes'),
                  controller: _minutes,
                  keyboardType: TextInputType.number,
                  inputFormatters: [FilteringTextInputFormatter.digitsOnly],
                  decoration: const InputDecoration(
                    labelText: 'Minutes to approve',
                    suffixText: 'min',
                    border: OutlineInputBorder(),
                  ),
                  validator: (v) => validateApprovedMinutes(v, widget.day.computedMinutes),
                ),
                const SizedBox(height: 12),
                TextFormField(
                  controller: _comment,
                  maxLength: 200,
                  decoration: const InputDecoration(
                    labelText: 'Comment (optional)',
                    border: OutlineInputBorder(),
                  ),
                  validator: (v) => validateClaimComment(v, required: false),
                ),
              ],
            ),
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            key: const ValueKey('ot-approve-ok'),
            onPressed: () {
              if (!(_form.currentState?.validate() ?? false)) return;
              final c = _comment.text.trim();
              Navigator.pop(context, (minutes: int.parse(_minutes.text.trim()), comment: c.isEmpty ? null : c));
            },
            child: const Text('Approve'),
          ),
        ],
      );
}
