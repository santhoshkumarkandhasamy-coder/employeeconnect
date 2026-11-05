import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/security/screen_protect.dart';
import '../../admin/presentation/admin_widgets.dart' show adminPermissionsProvider;
import '../domain/payroll_models.dart' show canManagePayroll;
import 'bank_accounts_section.dart';
import 'delivery_report_screen.dart';
import 'payment_batches_screen.dart';
import 'payment_widgets.dart';
import 'payroll_providers.dart';

/// Payments area of the payroll console: batches | bank accounts | delivery.
class PaymentsTab extends ConsumerStatefulWidget {
  const PaymentsTab({super.key});

  @override
  ConsumerState<PaymentsTab> createState() => _PaymentsTabState();
}

enum _Part { batches, accounts, delivery }

class _PaymentsTabState extends ConsumerState<PaymentsTab> {
  _Part _part = _Part.batches;

  Future<void> _newBatch() async {
    await Navigator.of(context).push<void>(
      MaterialPageRoute<void>(builder: (_) => const _PickRunScreen()),
    );
    ref.invalidate(paymentBatchesProvider);
  }

  @override
  Widget build(BuildContext context) {
    final manage = canManagePayroll(ref.watch(adminPermissionsProvider));
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
          child: SegmentedButton<_Part>(
            key: const ValueKey('payments-parts'),
            showSelectedIcon: false,
            segments: const [
              ButtonSegment(value: _Part.batches, label: Text('Batches')),
              ButtonSegment(value: _Part.accounts, label: Text('Accounts')),
              ButtonSegment(value: _Part.delivery, label: Text('Delivery')),
            ],
            selected: {_part},
            onSelectionChanged: (s) => setState(() => _part = s.first),
          ),
        ),
        Expanded(
          child: switch (_part) {
            _Part.batches => Scaffold(
                backgroundColor: Colors.transparent,
                floatingActionButton: manage
                    ? FloatingActionButton.extended(
                        key: const ValueKey('payments-new-batch'),
                        onPressed: _newBatch,
                        icon: const Icon(Icons.add),
                        label: const Text('New batch'),
                      )
                    : null,
                body: const PaymentBatchesList(),
              ),
            _Part.accounts => const BankAccountsSection(),
            _Part.delivery => PublishedRunsPicker(
                onPick: (run) => Navigator.of(context).push<void>(
                  MaterialPageRoute<void>(
                    builder: (_) => DeliveryReportScreen(runId: run.id, period: run.period),
                  ),
                ),
              ),
          },
        ),
      ],
    );
  }
}

/// Choose a published run, then continue with "new batch" for it.
class _PickRunScreen extends StatelessWidget {
  const _PickRunScreen();

  @override
  Widget build(BuildContext context) => SecureScreen(
        child: Scaffold(
          appBar: AppBar(title: const Text('Choose a published run')),
          body: PublishedRunsPicker(
            onPick: (run) => Navigator.of(context).pushReplacement<void, void>(
              MaterialPageRoute<void>(builder: (_) => CreateBatchScreen(runId: run.id, period: run.period)),
            ),
          ),
        ),
      );
}
