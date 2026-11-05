import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import '../../admin/presentation/admin_widgets.dart' show adminSelfIdProvider;
import '../domain/payroll_models.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// Months from [back] before to [ahead] after [center] (`YYYY-MM`), oldest first.
List<String> monthOptions(String center, {int back = 1, int ahead = 12}) =>
    [for (var i = -back; i <= ahead; i++) shiftMonth(center, i)];

/// `2026-11`, always among the options (an item can sit in an older month).
List<String> monthOptionsWith(String center, String? extra, {int back = 1, int ahead = 12}) {
  final base = monthOptions(center, back: back, ahead: ahead);
  if (extra != null && extra.isNotEmpty && !base.contains(extra)) base.insert(0, extra);
  return base;
}

/// Month dropdown for target months of pay items.
class MonthDropdown extends StatelessWidget {
  const MonthDropdown({
    required this.label,
    required this.value,
    required this.options,
    required this.onChanged,
    super.key,
  });
  final String label;
  final String? value;
  final List<String> options;
  final ValueChanged<String?> onChanged;

  @override
  Widget build(BuildContext context) => DropdownButtonFormField<String>(
        initialValue: options.contains(value) ? value : null,
        isExpanded: true,
        decoration: InputDecoration(labelText: label, border: const OutlineInputBorder()),
        items: [for (final m in options) DropdownMenuItem(value: m, child: Text(monthTitle(m)))],
        onChanged: onChanged,
        validator: (v) => v == null ? 'Choose a month' : null,
      );
}

/// Previous / next month bar. [min] / [max] bound the chevrons.
class PayMonthBar extends StatelessWidget {
  const PayMonthBar({
    required this.period,
    required this.onChanged,
    super.key,
    this.keyPrefix = 'pay',
    this.min,
    this.max,
  });
  final String period;
  final ValueChanged<String> onChanged;
  final String keyPrefix;
  final String? min;
  final String? max;

  @override
  Widget build(BuildContext context) => Row(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          IconButton(
            key: ValueKey('$keyPrefix-prev'),
            tooltip: 'Previous month',
            icon: const Icon(Icons.chevron_left),
            onPressed: min != null && period.compareTo(min!) <= 0
                ? null
                : () => onChanged(shiftMonth(period, -1)),
          ),
          Semantics(
            liveRegion: true,
            child: Text(periodTitle(period), style: Theme.of(context).textTheme.titleMedium),
          ),
          IconButton(
            key: ValueKey('$keyPrefix-next'),
            tooltip: 'Next month',
            icon: const Icon(Icons.chevron_right),
            onPressed: max != null && period.compareTo(max!) >= 0
                ? null
                : () => onChanged(shiftMonth(period, 1)),
          ),
        ],
      );
}

/// Runs [action]; shows the payroll error text in a snack bar and returns
/// false on an [ApiException]. A cancelled password prompt is not an error.
Future<bool> payGuard(BuildContext context, Future<bool> Function() action) async {
  try {
    return await action();
  } on ApiException catch (e) {
    if (context.mounted) payrollSnack(context, payrollErrorMessage(e));
    return false;
  }
}

/// Tappable form field that opens the employee search dialog.
class EmployeePickerField extends FormField<EmployeeRef> {
  EmployeePickerField({
    required ValueChanged<EmployeeRef> onPicked,
    super.key,
    EmployeeRef? initial,
    String label = 'Employee',
  }) : super(
          initialValue: initial,
          validator: (v) => v == null ? 'Choose an employee' : null,
          builder: (state) => InkWell(
            onTap: () async {
              final picked = await showDialog<EmployeeRef>(
                context: state.context,
                builder: (_) => const EmployeePickerDialog(),
              );
              if (picked != null) {
                state.didChange(picked);
                onPicked(picked);
              }
            },
            child: InputDecorator(
              decoration: InputDecoration(
                labelText: label,
                border: const OutlineInputBorder(),
                errorText: state.errorText,
                suffixIcon: const Icon(Icons.search),
              ),
              child: Text(state.value?.display ?? 'Tap to choose'),
            ),
          ),
        );
}

/// Search box over the payroll employee list. The signed-in user is left out
/// (nobody can change their own payroll data).
class EmployeePickerDialog extends ConsumerStatefulWidget {
  const EmployeePickerDialog({super.key});

  @override
  ConsumerState<EmployeePickerDialog> createState() => _EmployeePickerDialogState();
}

class _EmployeePickerDialogState extends ConsumerState<EmployeePickerDialog> {
  final _ctl = TextEditingController();
  Timer? _debounce;
  String _q = '';

  @override
  void dispose() {
    _debounce?.cancel();
    _ctl.dispose();
    super.dispose();
  }

  void _onChanged(String v) {
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 300), () {
      if (mounted) setState(() => _q = v.trim());
    });
  }

  @override
  Widget build(BuildContext context) {
    final self = ref.watch(adminSelfIdProvider);
    final async = ref.watch(payrollEmployeesProvider(PayrollEmployeeQuery(q: _q)));
    return AlertDialog(
      title: const Text('Choose an employee'),
      content: SizedBox(
        width: 400,
        height: 360,
        child: Column(
          children: [
            TextField(
              key: const ValueKey('employee-search'),
              controller: _ctl,
              autofocus: true,
              onChanged: _onChanged,
              decoration: const InputDecoration(
                labelText: 'Search by name or employee ID',
                prefixIcon: Icon(Icons.search),
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 8),
            Expanded(
              child: async.when(
                skipLoadingOnReload: true,
                loading: () => const LoadingView(),
                error: (e, _) => ErrorView(
                  message: payrollMsg(e),
                  onRetry: () => ref.invalidate(payrollEmployeesProvider),
                ),
                data: (list) {
                  final items = [for (final e in list.items) if (e.user.id != self) e];
                  if (items.isEmpty) return const EmptyView(message: 'No employees found.');
                  return ListView.builder(
                    itemCount: items.length,
                    itemBuilder: (_, i) => ListTile(
                      key: ValueKey('pick-${items[i].user.id}'),
                      dense: true,
                      title: Text(items[i].user.fullName),
                      subtitle: Text(items[i].user.employeeId),
                      onTap: () => Navigator.pop(context, items[i].user),
                    ),
                  );
                },
              ),
            ),
          ],
        ),
      ),
      actions: [TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel'))],
    );
  }
}

/// Small coloured status chip for pay item rows.
class PayStatusChip extends StatelessWidget {
  const PayStatusChip(this.label, {super.key, this.tone = PayTone.neutral});
  final String label;
  final PayTone tone;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final (bg, fg) = switch (tone) {
      PayTone.good => (cs.primaryContainer, cs.onPrimaryContainer),
      PayTone.warn => (cs.tertiaryContainer, cs.onTertiaryContainer),
      PayTone.bad => (cs.errorContainer, cs.onErrorContainer),
      PayTone.neutral => (cs.surfaceContainerHighest, cs.onSurfaceVariant),
    };
    return Chip(
      label: Text(label),
      backgroundColor: bg,
      labelStyle: TextStyle(color: fg),
      visualDensity: VisualDensity.compact,
      side: BorderSide.none,
      padding: EdgeInsets.zero,
    );
  }
}

enum PayTone { good, warn, bad, neutral }

PayTone payToneFor(String status) => switch (status) {
      'paid' || 'deducted' || 'closed' => PayTone.good,
      'pending' || 'proposed' || 'processing' || 'scheduled' || 'active' => PayTone.warn,
      'cancelled' || 'dismissed' => PayTone.bad,
      _ => PayTone.neutral,
    };

/// Small bar of text lines under a title (flags and run hints).
Widget payNotes(BuildContext context, Iterable<String> lines) {
  final style = Theme.of(context).textTheme.bodySmall;
  return Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [for (final l in lines) Text(l, style: style)],
  );
}

/// Plain section header row with an optional action.
class PaySectionHeader extends StatelessWidget {
  const PaySectionHeader({required this.title, super.key, this.actions = const []});
  final String title;
  final List<Widget> actions;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(12, 4, 12, 0),
        child: Wrap(
          crossAxisAlignment: WrapCrossAlignment.center,
          spacing: 8,
          runSpacing: 4,
          children: [
            Text(title, style: Theme.of(context).textTheme.titleMedium),
            ...actions,
          ],
        ),
      );
}

/// Loading / error / empty / data body for a plain [AsyncValue].
Widget payAsync<T>(
  BuildContext context,
  AsyncValue<T> async, {
  required VoidCallback onRetry,
  required String emptyMessage,
  required bool Function(T) isEmpty,
  required Widget Function(T) builder,
  IconData emptyIcon = Icons.inbox_outlined,
  String loadingLabel = 'Loading',
}) =>
    async.when(
      skipLoadingOnReload: true,
      loading: () => LoadingView(label: loadingLabel),
      error: (e, _) => ErrorView(message: payrollMsg(e), onRetry: onRetry),
      data: (d) => isEmpty(d)
          ? ListView(
              physics: const AlwaysScrollableScrollPhysics(),
              children: [const SizedBox(height: 80), EmptyView(icon: emptyIcon, message: emptyMessage)],
            )
          : builder(d),
    );
