import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart' show NoPermissionScreen, adminPermissionsProvider;
import '../domain/payroll_models.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';
import 'payslip_detail_screen.dart';

/// "Payslips" under More: the signed-in user's published payslips, one row per
/// month. The list carries no amounts by contract; they appear only in the
/// detail. Screenshot-protected; state is in memory only (never json_cache).
class PayslipsScreen extends ConsumerStatefulWidget {
  const PayslipsScreen({super.key});

  @override
  ConsumerState<PayslipsScreen> createState() => _PayslipsScreenState();
}

class _PayslipsScreenState extends ConsumerState<PayslipsScreen> {
  late int _year = ref.read(payrollClockProvider)().year;

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!canViewOwnPayslips(perms)) return const NoPermissionScreen(title: 'Payslips');
    final now = ref.read(payrollClockProvider)();
    final async = ref.watch(myPayslipsProvider(_year));
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Payslips')),
        body: Column(
          children: [
            YearSelector(
              year: _year,
              maxYear: now.year,
              onChanged: (y) => setState(() => _year = y),
            ),
            Expanded(
              child: async.when(
                skipLoadingOnReload: true,
                loading: () => const LoadingView(label: 'Loading payslips'),
                error: (e, _) => ErrorView(
                  message: payrollMsg(e),
                  onRetry: () => ref.invalidate(myPayslipsProvider(_year)),
                ),
                data: (items) => RefreshIndicator(
                  onRefresh: () async {
                    ref.invalidate(myPayslipsProvider(_year));
                    await ref.read(myPayslipsProvider(_year).future).then((_) {}, onError: (Object _) {});
                  },
                  child: items.isEmpty
                      ? ListView(
                          physics: const AlwaysScrollableScrollPhysics(),
                          children: const [
                            SizedBox(height: 80),
                            EmptyView(
                              icon: Icons.receipt_long_outlined,
                              message: 'No payslips yet',
                            ),
                          ],
                        )
                      : ListView.builder(
                          physics: const AlwaysScrollableScrollPhysics(),
                          padding: const EdgeInsets.fromLTRB(12, 4, 12, 24),
                          itemCount: items.length,
                          itemBuilder: (_, i) => PayslipTile(item: items[i]),
                        ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// One month of the list: period, published date, "Revised" chip. No amounts.
class PayslipTile extends StatelessWidget {
  const PayslipTile({required this.item, super.key});
  final PayslipListItem item;

  @override
  Widget build(BuildContext context) {
    final published = item.publishedAt;
    final pay = parseYmd(item.payDate);
    return Card(
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: ListTile(
        leading: const Icon(Icons.receipt_long_outlined),
        title: Text(periodTitle(item.period)),
        subtitle: Text(
          [
            if (published != null) 'Published ${formatDayLong(published)}',
            if (pay != null) 'Pay date ${formatDayLong(pay)}',
          ].join('\n'),
        ),
        isThreeLine: published != null && pay != null,
        trailing: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (item.revised)
              const Chip(
                label: Text('Revised'),
                visualDensity: VisualDensity.compact,
                padding: EdgeInsets.zero,
              ),
            const Icon(Icons.chevron_right),
          ],
        ),
        onTap: () => Navigator.of(context).push(
          MaterialPageRoute<void>(builder: (_) => PayslipDetailScreen(id: item.id)),
        ),
      ),
    );
  }
}
