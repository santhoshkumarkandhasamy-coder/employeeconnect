import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../data/payroll_delivery.dart';
import '../data/payroll_repository.dart';
import '../domain/payroll_logic.dart';
import '../domain/payroll_models.dart';
import '../domain/payroll_money.dart';

/// Message for any error thrown by a payroll call.
String payrollMsg(Object e) =>
    e is ApiException ? payrollErrorMessage(e) : 'Something went wrong. Please try again.';

void payrollSnack(BuildContext context, String message) {
  ScaffoldMessenger.of(context)
    ..hideCurrentSnackBar()
    ..showSnackBar(SnackBar(content: Text(message), duration: const Duration(seconds: 6)));
}

/// Fetches a file (PDF / workbook) and hands it to the platform delivery
/// (share sheet on the phone, browser download in the console). Everything
/// needed from [ref] is read before the first await.
Future<void> downloadPayrollFile(
  BuildContext context,
  WidgetRef ref,
  Future<PayrollFile?> Function(PayrollRepository repo) fetch,
) async {
  final messenger = ScaffoldMessenger.of(context);
  final repo = ref.read(payrollRepositoryProvider);
  final delivery = ref.read(payrollDeliveryProvider);
  void say(String m) => messenger
    ..hideCurrentSnackBar()
    ..showSnackBar(SnackBar(content: Text(m), duration: const Duration(seconds: 6)));
  try {
    final file = await fetch(repo);
    if (file == null || !context.mounted) return; // null = the user cancelled
    final r = await delivery.deliver(context, file);
    switch (r) {
      case DeliveryOutcome.done:
        break;
      case DeliveryOutcome.cancelled:
        say('Cancelled. The file was not kept.');
      case DeliveryOutcome.unavailable:
        say('Sharing is not available on this device. The file was deleted.');
    }
  } on ApiException catch (e) {
    say(payrollErrorMessage(e));
  } on Object {
    say('Could not save the file.');
  }
}

/// Dialog listing the employees that block processing (`PAYROLL_BLOCKED`).
Future<void> showBlockedDialog(BuildContext context, ApiException e) => showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Payroll cannot be processed'),
        content: SizedBox(
          width: double.maxFinite,
          child: e.details.isEmpty
              ? const Text('Some payslips have errors. Fix them and try again.')
              : ListView(
                  shrinkWrap: true,
                  children: [
                    const Text('Fix these payslips, or put them on hold, then process again.'),
                    const SizedBox(height: 8),
                    for (final d in e.details)
                      ListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        leading: const Icon(Icons.error_outline),
                        title: Text(d.field),
                        subtitle: Text(payrollFlagLabel(d.message)),
                      ),
                  ],
                ),
        ),
        actions: [
          FilledButton(
            onPressed: () => Navigator.pop(ctx),
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: const Text('OK'),
          ),
        ],
      ),
    );

/// Asks for a text of 3..200 characters (revision reason). Null = cancelled.
Future<String?> showReasonDialog(
  BuildContext context, {
  required String title,
  required String hint,
  required String confirmLabel,
}) =>
    showDialog<String>(
      context: context,
      builder: (_) => _ReasonDialog(title: title, hint: hint, confirmLabel: confirmLabel),
    );

class _ReasonDialog extends StatefulWidget {
  const _ReasonDialog({required this.title, required this.hint, required this.confirmLabel});
  final String title;
  final String hint;
  final String confirmLabel;

  @override
  State<_ReasonDialog> createState() => _ReasonDialogState();
}

class _ReasonDialogState extends State<_ReasonDialog> {
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
          child: TextFormField(
            key: const ValueKey('reason-field'),
            controller: _text,
            autofocus: true,
            maxLength: 200,
            maxLines: 3,
            minLines: 1,
            decoration: InputDecoration(
              labelText: widget.hint,
              border: const OutlineInputBorder(),
            ),
            validator: (v) => validateLopReason(v, overrideSet: true),
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            onPressed: () {
              if (_form.currentState?.validate() ?? false) Navigator.pop(context, _text.text.trim());
            },
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: Text(widget.confirmLabel),
          ),
        ],
      );
}

/// Amount input: decimal keyboard, validated, sent as a string.
class MoneyField extends StatelessWidget {
  const MoneyField({
    required this.controller,
    required this.label,
    super.key,
    this.required = true,
    this.positive = false,
    this.enabled = true,
    this.onChanged,
    this.helper,
  });
  final TextEditingController controller;
  final String label;
  final bool required;
  final bool positive;
  final bool enabled;
  final VoidCallback? onChanged;
  final String? helper;

  @override
  Widget build(BuildContext context) => TextFormField(
        controller: controller,
        enabled: enabled,
        keyboardType: const TextInputType.numberWithOptions(decimal: true),
        inputFormatters: [FilteringTextInputFormatter.allow(RegExp(r'[0-9.]'))],
        textInputAction: TextInputAction.next,
        onChanged: onChanged == null ? null : (_) => onChanged!(),
        decoration: InputDecoration(
          labelText: label,
          helperText: helper,
          prefixText: '₹ ',
          border: const OutlineInputBorder(),
        ),
        validator: (v) => validateMoney(v, required: required, positive: positive, label: label),
      );
}

/// A label / amount line used by the payslip views.
class AmountRow extends StatelessWidget {
  const AmountRow({
    required this.label,
    required this.value,
    super.key,
    this.bold = false,
    this.caption,
  });
  final String label;
  final String value;
  final bool bold;
  final String? caption;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final style = bold ? theme.textTheme.titleMedium : theme.textTheme.bodyMedium;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(label, style: style),
                if (caption != null) Text(caption!, style: theme.textTheme.bodySmall),
              ],
            ),
          ),
          const SizedBox(width: 12),
          Text(value, style: style),
        ],
      ),
    );
  }
}

class SectionCard extends StatelessWidget {
  const SectionCard({required this.title, required this.children, super.key});
  final String title;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) => Card(
        margin: const EdgeInsets.symmetric(vertical: 6),
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Semantics(header: true, child: Text(title, style: Theme.of(context).textTheme.titleSmall)),
              const SizedBox(height: 6),
              ...children,
            ],
          ),
        ),
      );
}

/// Draft / processed / published chip.
class StatusChip extends StatelessWidget {
  const StatusChip(this.status, {super.key});
  final String status;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final (bg, fg) = switch (status) {
      'published' => (cs.primaryContainer, cs.onPrimaryContainer),
      'processed' => (cs.secondaryContainer, cs.onSecondaryContainer),
      'superseded' => (cs.surfaceContainerHighest, cs.onSurfaceVariant),
      _ => (cs.tertiaryContainer, cs.onTertiaryContainer),
    };
    return Chip(
      label: Text(payrollStatusLabel(status)),
      backgroundColor: bg,
      labelStyle: TextStyle(color: fg),
      visualDensity: VisualDensity.compact,
      side: BorderSide.none,
      padding: EdgeInsets.zero,
    );
  }
}

/// Error (red) and warning (neutral) chips for payslip flags.
class FlagChips extends StatelessWidget {
  const FlagChips({super.key, this.errors = const [], this.warnings = const [], this.hold = false});
  final List<String> errors;
  final List<String> warnings;
  final bool hold;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Wrap(
      spacing: 6,
      runSpacing: 4,
      children: [
        if (hold)
          const Chip(
            avatar: Icon(Icons.pause_circle_outline, size: 16),
            label: Text('On hold'),
            visualDensity: VisualDensity.compact,
          ),
        for (final e in errors)
          Chip(
            avatar: Icon(Icons.error_outline, size: 16, color: cs.onErrorContainer),
            label: Text(payrollFlagLabel(e)),
            backgroundColor: cs.errorContainer,
            labelStyle: TextStyle(color: cs.onErrorContainer),
            visualDensity: VisualDensity.compact,
            side: BorderSide.none,
          ),
        for (final w in warnings)
          Chip(
            avatar: const Icon(Icons.warning_amber_outlined, size: 16),
            label: Text(payrollFlagLabel(w)),
            visualDensity: VisualDensity.compact,
          ),
      ],
    );
  }
}

/// `Earnings` / `Deductions` table body shared by the employee and admin views.
List<Widget> payslipLineRows(List<PayslipLine> lines, {bool showMonthly = false}) => [
      for (final l in lines)
        AmountRow(
          label: l.label.isEmpty ? l.code : l.label,
          value: formatRupees(l.amount),
          caption: showMonthly && l.monthly != null ? 'Monthly ${formatRupees(l.monthly)}' : null,
        ),
    ];

/// Year selector (chevrons) for the payslip and run lists.
class YearSelector extends StatelessWidget {
  const YearSelector({required this.year, required this.onChanged, required this.maxYear, super.key});
  final int year;
  final int maxYear;
  final ValueChanged<int> onChanged;

  @override
  Widget build(BuildContext context) => Row(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          IconButton(
            tooltip: 'Previous year',
            icon: const Icon(Icons.chevron_left),
            onPressed: year > 2000 ? () => onChanged(year - 1) : null,
          ),
          Semantics(
            liveRegion: true,
            child: Text('$year', style: Theme.of(context).textTheme.titleMedium),
          ),
          IconButton(
            tooltip: 'Next year',
            icon: const Icon(Icons.chevron_right),
            onPressed: year < maxYear ? () => onChanged(year + 1) : null,
          ),
        ],
      );
}
