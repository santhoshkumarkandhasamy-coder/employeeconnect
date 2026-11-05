import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/utils/dates.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import '../../admin/presentation/admin_widgets.dart' show AdminPagedBody;
import '../data/payroll_repository.dart';
import '../domain/pay_items_models.dart';
import '../domain/payroll_logic.dart';
import '../domain/payroll_models.dart' show periodTitle;
import '../domain/payroll_money.dart';
import 'import_wizard_screen.dart';
import 'pay_items_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// One-time items (bonus, incentive, other earning / deduction): month list
/// with filters, add / edit / cancel, bulk import and batches (contract 17.2).
class OneTimeSection extends ConsumerStatefulWidget {
  const OneTimeSection({required this.canManage, super.key});
  final bool canManage;

  @override
  ConsumerState<OneTimeSection> createState() => _OneTimeSectionState();
}

class _OneTimeSectionState extends ConsumerState<OneTimeSection> {
  late String _period;
  late String _thisMonth;
  String? _code;
  String? _status;

  @override
  void initState() {
    super.initState();
    _thisMonth = monthKey(ref.read(payrollClockProvider)());
    _period = _thisMonth;
  }

  ItemQuery get _query => ItemQuery(period: _period, code: _code, status: _status);

  void _reload() => ref.invalidate(oneTimeItemsProvider);

  Future<void> _add() async {
    final r = await showDialog<ItemFormResult>(
      context: context,
      builder: (_) => ItemFormDialog(thisMonth: _thisMonth, initialPeriod: _period),
    );
    if (r == null || !mounted) return;
    final repo = ref.read(payrollRepositoryProvider);
    final ok = await payGuard(context, () async {
      final item = await repo.createItem(
        userId: r.user!.id,
        period: r.period,
        code: r.code,
        label: r.label,
        amount: r.amount,
      );
      if (mounted) {
        payrollSnack(
          context,
          [
            'Item added for ${r.user!.fullName}.',
            if (runStatusHint(item.runStatus) != null) '${runStatusHint(item.runStatus)}.',
          ].join(' '),
        );
      }
      return true;
    });
    if (ok) _reload();
  }

  Future<void> _edit(OneTimeItem item) async {
    final r = await showDialog<ItemFormResult>(
      context: context,
      builder: (_) => ItemFormDialog(thisMonth: _thisMonth, initialPeriod: item.period, item: item),
    );
    if (r == null || !mounted) return;
    final repo = ref.read(payrollRepositoryProvider);
    await payGuard(context, () async {
      await repo.updateItem(
        item.id,
        expectedVersion: item.version,
        period: r.period == item.period ? null : r.period,
        label: r.label == item.label ? null : r.label,
        amount: r.amount == item.amount ? null : r.amount,
      );
      if (mounted) payrollSnack(context, 'Item updated.');
      return true;
    });
    // VERSION_CONFLICT / ITEM_LOCKED mean the row changed: reload either way.
    _reload();
  }

  Future<void> _cancel(OneTimeItem item) async {
    final reason = await showReasonDialog(
      context,
      title: 'Cancel ${item.label}?',
      hint: 'Reason (3 to 200 characters)',
      confirmLabel: 'Cancel item',
    );
    if (reason == null || !mounted) return;
    final repo = ref.read(payrollRepositoryProvider);
    await payGuard(context, () async {
      await repo.cancelItem(item.id, reason);
      if (mounted) payrollSnack(context, 'Item cancelled.');
      return true;
    });
    _reload();
  }

  Future<void> _import() async {
    await Navigator.of(context).push<void>(
      MaterialPageRoute<void>(builder: (_) => ImportWizardScreen(initialPeriod: _period)),
    );
    _reload();
    ref.invalidate(importBatchesProvider);
  }

  Future<void> _batches() async {
    await Navigator.of(context).push<void>(
      MaterialPageRoute<void>(
        builder: (_) => ImportBatchesScreen(initialPeriod: _period, canManage: widget.canManage),
      ),
    );
    _reload();
  }

  @override
  Widget build(BuildContext context) {
    final q = _query;
    final async = ref.watch(oneTimeItemsProvider(q));
    return Column(
      children: [
        PayMonthBar(
          keyPrefix: 'items',
          period: _period,
          onChanged: (p) => setState(() => _period = p),
          max: shiftMonth(_thisMonth, 12),
        ),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12),
          child: Wrap(
            spacing: 8,
            runSpacing: 8,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              _filter(
                key: 'items-code',
                label: 'Type',
                value: _code,
                options: {for (final c in oneTimeCodes) c: oneTimeCodeLabel(c)},
                onChanged: (v) => setState(() => _code = v),
              ),
              _filter(
                key: 'items-status',
                label: 'Status',
                value: _status,
                options: const {
                  'pending': 'Pending',
                  'processing': 'In payslip',
                  'paid': 'Paid',
                  'cancelled': 'Cancelled',
                },
                onChanged: (v) => setState(() => _status = v),
              ),
              if (widget.canManage)
                FilledButton.icon(
                  key: const ValueKey('items-add'),
                  onPressed: _add,
                  icon: const Icon(Icons.add),
                  label: const Text('Add item'),
                ),
              if (widget.canManage)
                OutlinedButton.icon(
                  key: const ValueKey('items-import'),
                  onPressed: _import,
                  icon: const Icon(Icons.upload_file_outlined),
                  label: const Text('Import'),
                ),
              TextButton.icon(
                key: const ValueKey('items-batches'),
                onPressed: _batches,
                icon: const Icon(Icons.history),
                label: const Text('Batches'),
              ),
            ],
          ),
        ),
        Expanded(
          child: AdminPagedBody<OneTimeItem>(
            async: async,
            emptyMessage: 'No pay items for ${periodTitle(_period)}.',
            emptyIcon: Icons.card_giftcard_outlined,
            onRefresh: () async => ref.read(oneTimeItemsProvider(q).notifier).reload(),
            onLoadMore: () => ref.read(oneTimeItemsProvider(q).notifier).loadMore(),
            itemBuilder: (context, item) => _ItemCard(
              item: item,
              canManage: widget.canManage,
              onEdit: () => _edit(item),
              onCancel: () => _cancel(item),
            ),
          ),
        ),
      ],
    );
  }

  Widget _filter({
    required String key,
    required String label,
    required String? value,
    required Map<String, String> options,
    required ValueChanged<String?> onChanged,
  }) =>
      SizedBox(
        width: 150,
        child: DropdownButtonFormField<String?>(
          key: ValueKey(key),
          initialValue: value,
          isExpanded: true,
          decoration: InputDecoration(
            labelText: label,
            isDense: true,
            border: const OutlineInputBorder(),
          ),
          items: [
            const DropdownMenuItem<String?>(child: Text('All')),
            for (final e in options.entries) DropdownMenuItem<String?>(value: e.key, child: Text(e.value)),
          ],
          onChanged: onChanged,
        ),
      );
}

class _ItemCard extends StatelessWidget {
  const _ItemCard({
    required this.item,
    required this.canManage,
    required this.onEdit,
    required this.onCancel,
  });
  final OneTimeItem item;
  final bool canManage;
  final VoidCallback onEdit;
  final VoidCallback onCancel;

  @override
  Widget build(BuildContext context) {
    final hint = item.isPending ? runStatusHint(item.runStatus) : null;
    return Card(
      key: ValueKey('item-${item.id}'),
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(14, 10, 6, 10),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(item.label, style: Theme.of(context).textTheme.titleSmall),
                  Text('${item.user.display}  -  ${oneTimeCodeLabel(item.code)}'),
                  const SizedBox(height: 2),
                  Text(
                    '${item.isDeduction ? '-' : '+'} ${formatRupees(item.amount)}',
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                  payNotes(context, [
                    if (item.batchId != null) 'From a bulk import',
                    if (hint != null) hint,
                    for (final f in item.flags) payItemFlagLabel(f),
                  ]),
                  const SizedBox(height: 4),
                  PayStatusChip(payItemStatusLabel(item.status), tone: payToneFor(item.status)),
                ],
              ),
            ),
            if (canManage && item.isPending)
              PopupMenuButton<String>(
                key: ValueKey('item-menu-${item.id}'),
                tooltip: 'Actions',
                onSelected: (v) => v == 'edit' ? onEdit() : onCancel(),
                itemBuilder: (_) => const [
                  PopupMenuItem(value: 'edit', child: Text('Edit')),
                  PopupMenuItem(value: 'cancel', child: Text('Cancel item')),
                ],
              ),
          ],
        ),
      ),
    );
  }
}

class ItemFormResult {
  const ItemFormResult({
    required this.user,
    required this.period,
    required this.code,
    required this.label,
    required this.amount,
  });
  final EmployeeRef? user;
  final String period;
  final String code;
  final String label;
  final String amount;
}

/// Add (employee, month, type, label, amount) or edit (month, label, amount).
class ItemFormDialog extends StatefulWidget {
  const ItemFormDialog({
    required this.thisMonth,
    required this.initialPeriod,
    super.key,
    this.item,
  });
  final String thisMonth;
  final String initialPeriod;
  final OneTimeItem? item;

  @override
  State<ItemFormDialog> createState() => _ItemFormDialogState();
}

class _ItemFormDialogState extends State<ItemFormDialog> {
  final _form = GlobalKey<FormState>();
  late final _label = TextEditingController(text: widget.item?.label ?? '');
  late final _amount = TextEditingController(text: widget.item?.amount ?? '');
  late String _period = widget.initialPeriod;
  late String _code = widget.item?.code ?? oneTimeCodes.first;
  EmployeeRef? _user;

  bool get _editing => widget.item != null;

  @override
  void dispose() {
    _label.dispose();
    _amount.dispose();
    super.dispose();
  }

  void _submit() {
    if (!(_form.currentState?.validate() ?? false)) return;
    Navigator.pop(
      context,
      ItemFormResult(
        user: _user,
        period: _period,
        code: _code,
        label: _label.text.trim(),
        amount: normalizeMoney(_amount.text) ?? _amount.text.trim(),
      ),
    );
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: Text(_editing ? 'Edit pay item' : 'Add pay item'),
        content: SizedBox(
          width: 400,
          child: SingleChildScrollView(
            child: Form(
              key: _form,
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  if (!_editing) ...[
                    EmployeePickerField(
                      key: const ValueKey('item-employee'),
                      onPicked: (u) => _user = u,
                    ),
                    const SizedBox(height: 12),
                    DropdownButtonFormField<String>(
                      key: const ValueKey('item-code'),
                      initialValue: _code,
                      isExpanded: true,
                      decoration: const InputDecoration(labelText: 'Type', border: OutlineInputBorder()),
                      items: [
                        for (final c in oneTimeCodes)
                          DropdownMenuItem(value: c, child: Text(oneTimeCodeLabel(c))),
                      ],
                      onChanged: (v) => setState(() => _code = v ?? _code),
                    ),
                    const SizedBox(height: 12),
                  ],
                  MonthDropdown(
                    label: 'Month it is paid in',
                    value: _period,
                    options: monthOptionsWith(widget.thisMonth, widget.initialPeriod),
                    onChanged: (v) => setState(() => _period = v ?? _period),
                  ),
                  const SizedBox(height: 12),
                  TextFormField(
                    key: const ValueKey('item-label'),
                    controller: _label,
                    maxLength: 40,
                    decoration: const InputDecoration(
                      labelText: 'Label on the payslip',
                      border: OutlineInputBorder(),
                    ),
                    validator: (v) => validatePayrollLabel(v, label: 'Label'),
                  ),
                  MoneyField(
                    key: const ValueKey('item-amount'),
                    controller: _amount,
                    label: 'Amount',
                    positive: true,
                    helper: _code == 'OTHER_DEDUCTION'
                        ? 'Enter a positive amount; it is deducted'
                        : 'Enter a positive amount',
                  ),
                ],
              ),
            ),
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            key: const ValueKey('item-save'),
            onPressed: _submit,
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: const Text('Save'),
          ),
        ],
      );
}

/// Imports from a bulk upload, listed per month; cancelling affects pending
/// rows only (contract 17.2).
class ImportBatchesScreen extends ConsumerStatefulWidget {
  const ImportBatchesScreen({required this.initialPeriod, required this.canManage, super.key});
  final String initialPeriod;
  final bool canManage;

  @override
  ConsumerState<ImportBatchesScreen> createState() => _ImportBatchesScreenState();
}

class _ImportBatchesScreenState extends ConsumerState<ImportBatchesScreen> {
  late String _period = widget.initialPeriod;

  Future<void> _cancel(ImportBatch b) async {
    final reason = await showReasonDialog(
      context,
      title: 'Cancel this import?',
      hint: 'Reason (3 to 200 characters)',
      confirmLabel: 'Cancel import',
    );
    if (reason == null || !mounted) return;
    final repo = ref.read(payrollRepositoryProvider);
    await payGuard(context, () async {
      final r = await repo.cancelBatch(b.id, reason);
      if (mounted) {
        payrollSnack(
          context,
          r.notCancelled == 0
              ? 'Cancelled ${r.cancelled} items.'
              : 'Cancelled ${r.cancelled} items. ${r.notCancelled} could not be cancelled because they are already in a payslip.',
        );
      }
      return true;
    });
    ref.invalidate(importBatchesProvider(_period));
  }

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(importBatchesProvider(_period));
    return Scaffold(
      appBar: AppBar(title: const Text('Bulk imports')),
      body: Column(
        children: [
          PayMonthBar(keyPrefix: 'batch', period: _period, onChanged: (p) => setState(() => _period = p)),
          Expanded(
            child: payAsync(
              context,
              async,
              onRetry: () => ref.invalidate(importBatchesProvider(_period)),
              emptyMessage: 'No imports for ${periodTitle(_period)}.',
              isEmpty: (l) => l.isEmpty,
              builder: (list) => ListView.builder(
                padding: const EdgeInsets.fromLTRB(12, 4, 12, 24),
                itemCount: list.length,
                itemBuilder: (_, i) {
                  final b = list[i];
                  final c = b.counts;
                  return Card(
                    key: ValueKey('batch-${b.id}'),
                    child: Padding(
                      padding: const EdgeInsets.all(12),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            '${b.rows} rows${b.createdAt == null ? '' : '  -  ${formatDayLong(b.createdAt!)}'}',
                            style: Theme.of(context).textTheme.titleSmall,
                          ),
                          if (b.createdBy != null) Text('By ${b.createdBy!.display}'),
                          Text(
                            'Pending ${c['pending'] ?? 0}, in payslip ${c['processing'] ?? 0}, '
                            'paid ${c['paid'] ?? 0}, cancelled ${c['cancelled'] ?? 0}',
                          ),
                          Row(
                            children: [
                              if (b.isCancelled)
                                const PayStatusChip('Cancelled', tone: PayTone.bad),
                              const Spacer(),
                              if (widget.canManage && !b.isCancelled && b.pendingCount > 0)
                                TextButton(
                                  key: ValueKey('batch-cancel-${b.id}'),
                                  onPressed: () => _cancel(b),
                                  child: const Text('Cancel pending items'),
                                ),
                            ],
                          ),
                        ],
                      ),
                    ),
                  );
                },
              ),
            ),
          ),
        ],
      ),
    );
  }
}
