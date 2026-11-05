import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart' show NoPermissionScreen, adminPermissionsProvider;
import '../domain/pay_items_models.dart';
import '../domain/payroll_models.dart';
import '../domain/payroll_money.dart';
import 'pay_items_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// "My loans" (read-only), opened from the Payslips screen. Lists the signed-in
/// user's loans and advances with the outstanding balance and the next
/// instalment; the detail shows the schedule (scheduled / deducted / deferred).
/// Screenshot-protected; in memory only.
class MyLoansScreen extends ConsumerWidget {
  const MyLoansScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!canViewOwnPayslips(perms)) return const NoPermissionScreen(title: 'My loans');
    final async = ref.watch(myLoansProvider);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('My loans')),
        body: payAsync<List<MyLoan>>(
          context,
          async,
          loadingLabel: 'Loading loans',
          onRetry: () => ref.invalidate(myLoansProvider),
          emptyMessage: 'You have no loans or advances.',
          emptyIcon: Icons.account_balance_wallet_outlined,
          isEmpty: (l) => l.isEmpty,
          builder: (loans) => RefreshIndicator(
            onRefresh: () async {
              ref.invalidate(myLoansProvider);
              await ref.read(myLoansProvider.future).then((_) {}, onError: (Object _) {});
            },
            child: ListView.builder(
              physics: const AlwaysScrollableScrollPhysics(),
              padding: const EdgeInsets.fromLTRB(12, 8, 12, 24),
              itemCount: loans.length,
              itemBuilder: (_, i) {
                final l = loans[i];
                final next = l.nextInstallment;
                return Card(
                  key: ValueKey('my-loan-${l.id}'),
                  child: ListTile(
                    title: Text('${l.kindLabel} ${l.reference}'),
                    subtitle: Text(
                      'Outstanding ${formatRupees(l.outstanding)} of ${formatRupees(l.principal)}\n'
                      '${next == null ? 'No instalment due' : 'Next ${monthTitle(next.period)}: ${formatRupees(next.amount)}'}',
                    ),
                    isThreeLine: true,
                    trailing: PayStatusChip(payItemStatusLabel(l.status), tone: payToneFor(l.status)),
                    onTap: () => Navigator.of(context).push(
                      MaterialPageRoute<void>(builder: (_) => MyLoanDetailScreen(loanId: l.id)),
                    ),
                  ),
                );
              },
            ),
          ),
        ),
      ),
    );
  }
}

class MyLoanDetailScreen extends ConsumerWidget {
  const MyLoanDetailScreen({required this.loanId, super.key});
  final String loanId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(myLoanProvider(loanId));
    final theme = Theme.of(context);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Loan details')),
        body: async.when(
          skipLoadingOnReload: true,
          loading: () => const LoadingView(label: 'Loading loan'),
          error: (e, _) => ErrorView(message: payrollMsg(e), onRetry: () => ref.invalidate(myLoanProvider(loanId))),
          data: (l) => ListView(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
            children: [
              Row(
                children: [
                  Expanded(child: Text('${l.kindLabel} ${l.reference}', style: theme.textTheme.titleMedium)),
                  PayStatusChip(payItemStatusLabel(l.status), tone: payToneFor(l.status)),
                ],
              ),
              SectionCard(
                title: 'Amounts',
                children: [
                  AmountRow(label: 'Amount', value: formatRupees(l.principal)),
                  AmountRow(label: 'Monthly deduction', value: formatRupees(l.emi)),
                  AmountRow(label: 'Outstanding', value: formatRupees(l.outstanding), bold: true),
                ],
              ),
              SectionCard(
                title: 'Schedule',
                children: [
                  if (l.installments.isEmpty) const Text('No instalments.'),
                  for (final (n, i) in l.installments.indexed)
                    AmountRow(
                      key: ValueKey('my-inst-$n'),
                      label: '${i.hasLiveNumber ? '${i.seq}. ' : ''}${monthTitle(i.period)}',
                      value: formatRupees(i.amount),
                      caption: myInstallmentLabels[i.status] ?? i.status,
                    ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}
