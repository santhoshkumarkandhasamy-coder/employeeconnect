import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../domain/payroll_models.dart' show periodTitle;
import 'fnf_widgets.dart' show lastDayLabel;
import 'pay_items_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';
import 'payslip_detail_screen.dart';

/// Employee: "Final settlement" card (contract 18.9). Shown only when the
/// server returns one of the user's own; carries no money. The caller gates it
/// on `payroll.view_own`. A failed read just hides the card (it is not needed
/// for payslips); pull to refresh retries.
class MyFnfCard extends ConsumerStatefulWidget {
  const MyFnfCard({super.key});

  @override
  ConsumerState<MyFnfCard> createState() => _MyFnfCardState();
}

class _MyFnfCardState extends ConsumerState<MyFnfCard> {
  bool _busy = false;

  Future<void> _download() async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await downloadPayrollFile(context, ref, (repo) => repo.myFnfStatement());
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final f = ref.watch(myFnfProvider).valueOrNull;
    if (f == null) return const SizedBox.shrink();
    final theme = Theme.of(context);
    return Card(
      key: const ValueKey('my-fnf-card'),
      margin: const EdgeInsets.fromLTRB(12, 4, 12, 4),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(child: Text('Final settlement', style: theme.textTheme.titleSmall)),
                PayStatusChip(
                  f.isSettled ? 'Settled' : 'In progress',
                  tone: f.isSettled ? PayTone.good : PayTone.warn,
                ),
              ],
            ),
            const SizedBox(height: 4),
            Text('Last working day ${lastDayLabel(f.lastWorkingDay)}'),
            Text('Settled in the payroll of ${periodTitle(f.settlementPeriod)}', style: theme.textTheme.bodySmall),
            if (!f.isSettled)
              Text(
                'HR is preparing your final settlement. The statement appears here when it is complete.',
                style: theme.textTheme.bodySmall,
              ),
            if (f.isSettled)
              Wrap(
                spacing: 8,
                children: [
                  FilledButton.icon(
                    key: const ValueKey('my-fnf-statement'),
                    onPressed: _busy ? null : _download,
                    icon: const Icon(Icons.download_outlined),
                    label: const Text('Download statement'),
                  ),
                  if (f.finalPayslipId != null)
                    TextButton(
                      key: const ValueKey('my-fnf-payslip'),
                      onPressed: () => Navigator.of(context).push<void>(
                        MaterialPageRoute<void>(builder: (_) => PayslipDetailScreen(id: f.finalPayslipId!)),
                      ),
                      child: const Text('Open final payslip'),
                    ),
                ],
              ),
          ],
        ),
      ),
    );
  }
}
