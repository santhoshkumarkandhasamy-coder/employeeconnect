import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart' show adminPermissionsProvider, runWithReauth;
import '../data/payroll_repository.dart';
import '../domain/payroll_models.dart';
import '../domain/payroll_money.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

String _periodOf(DateTime d) => '${d.year}-${d.month.toString().padLeft(2, '0')}';

const _reports = <(String, String)>[
  ('department', 'Department salary summary'),
  ('headcount', 'Headcount cost'),
  ('ctc', 'Cost to company'),
  ('lop', 'LOP report'),
  ('bank_advice', 'Bank advice'),
];

const _reasons = <String, String>{
  'NEW': 'New this month',
  'LEFT_OR_NOT_PAID': 'Left / not paid',
  'STRUCTURE_CHANGE': 'Salary structure changed',
  'LOP_CHANGE': 'LOP changed',
  'ADJUSTMENTS': 'Has adjustments',
  'PT_CHANGE': 'Professional tax changed',
};

const _alertText = <String, String>{
  'RUN_NOT_CREATED': 'The payroll run for this month has not been created and the process day has passed.',
  'RUN_NOT_PROCESSED': 'The payroll run is not processed yet and the process day has passed.',
  'RUN_NOT_PUBLISHED': 'The run is processed but not published and the pay date has arrived.',
};

/// Payroll dashboard (plan 14 phase 1): month totals, department cost, 12-month
/// trend, joiners / leavers, calendar alerts, variance vs last month, reports.
class PayrollDashboardTab extends ConsumerStatefulWidget {
  const PayrollDashboardTab({super.key});

  @override
  ConsumerState<PayrollDashboardTab> createState() => _PayrollDashboardTabState();
}

class _PayrollDashboardTabState extends ConsumerState<PayrollDashboardTab> {
  late DateTime _month = DateTime(DateTime.now().year, DateTime.now().month);

  void _shift(int d) => setState(() => _month = DateTime(_month.year, _month.month + d));

  bool get _isCurrent => _month.year == DateTime.now().year && _month.month == DateTime.now().month;

  Future<void> _download(String kind) async {
    final period = _periodOf(_month);
    await downloadPayrollFile(context, ref, (repo) async {
      if (kind != 'bank_advice') return repo.report(kind, period);
      PayrollFile? file;
      final done = await runWithReauth(
        context,
        reason: 'Bank details need your password.',
        action: (pw) async => file = await repo.report(kind, period, currentPassword: pw),
      );
      return done ? file : null;
    });
  }

  @override
  Widget build(BuildContext context) {
    final period = _periodOf(_month);
    final async = ref.watch(payrollDashboardProvider(period));
    final canBank = canManagePayroll(ref.watch(adminPermissionsProvider));
    return Column(
      children: [
        Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            IconButton(
              key: const ValueKey('dash-prev'),
              icon: const Icon(Icons.chevron_left),
              onPressed: () => _shift(-1),
            ),
            Text(periodTitle(period), style: Theme.of(context).textTheme.titleMedium),
            IconButton(
              key: const ValueKey('dash-next'),
              icon: const Icon(Icons.chevron_right),
              onPressed: _isCurrent ? null : () => _shift(1),
            ),
          ],
        ),
        Expanded(
          child: async.when(
            skipLoadingOnReload: true,
            loading: () => const LoadingView(label: 'Loading dashboard'),
            error: (e, _) => ErrorView(
              message: payrollMsg(e),
              onRetry: () => ref.invalidate(payrollDashboardProvider(period)),
            ),
            data: (d) => RefreshIndicator(
              onRefresh: () async {
                ref
                  ..invalidate(payrollDashboardProvider(period))
                  ..invalidate(payrollVarianceProvider(period));
              },
              child: ListView(
                padding: const EdgeInsets.all(16),
                children: [
                  ..._alerts(d),
                  _tiles(d),
                  const SizedBox(height: 16),
                  _departments(d),
                  const SizedBox(height: 16),
                  _trend(d),
                  const SizedBox(height: 16),
                  _people('Joiners', d['joiners']),
                  _people('Leavers', d['leavers']),
                  const SizedBox(height: 16),
                  _VarianceCard(period: period),
                  const SizedBox(height: 16),
                  _reportsCard(canBank),
                ],
              ),
            ),
          ),
        ),
      ],
    );
  }

  List<Widget> _alerts(Map<String, dynamic> d) {
    final cal = (d['calendar'] as Map<String, dynamic>?) ?? const {};
    final out = <Widget>[];
    for (final a in (cal['alerts'] as List?) ?? const []) {
      out.add(
        Card(
          color: Theme.of(context).colorScheme.errorContainer,
          child: ListTile(
            leading: const Icon(Icons.warning_amber_outlined),
            title: Text(_alertText['$a'] ?? '$a'),
          ),
        ),
      );
    }
    out.add(
      Padding(
        padding: const EdgeInsets.only(bottom: 8),
        child: Text('Process by ${cal['process_by'] ?? '-'}  -  pay date ${cal['pay_date'] ?? '-'}'),
      ),
    );
    return out;
  }

  Widget _tiles(Map<String, dynamic> d) {
    final t = (d['totals'] as Map<String, dynamic>?) ?? const {};
    Widget tile(String label, String value) => Card(
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(label, style: Theme.of(context).textTheme.bodySmall),
                Text(value, style: Theme.of(context).textTheme.titleMedium),
              ],
            ),
          ),
        );
    return Wrap(
      spacing: 8,
      runSpacing: 8,
      children: [
        for (final e in [
          ('Employees paid', '${d['headcount'] ?? 0}'),
          ('Gross', formatRupees('${t['gross'] ?? '0.00'}')),
          ('Deductions', formatRupees('${t['deductions'] ?? '0.00'}')),
          ('Net pay', formatRupees('${t['net'] ?? '0.00'}')),
          ('Employer cost', formatRupees('${t['employer_cost'] ?? '0.00'}')),
          ('On hold', '${d['held'] ?? 0}'),
          ('Errors', '${d['errors'] ?? 0}'),
          ('Warnings', '${d['warnings'] ?? 0}'),
        ])
          SizedBox(width: 160, child: tile(e.$1, e.$2)),
      ],
    );
  }

  Widget _departments(Map<String, dynamic> d) {
    final list = ((d['departments'] as List?) ?? const []).cast<Map<String, dynamic>>();
    return Card(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Padding(padding: EdgeInsets.all(12), child: Text('Department cost')),
          if (list.isEmpty) const Padding(padding: EdgeInsets.all(12), child: Text('No data for this month.')),
          for (final r in list)
            ListTile(
              dense: true,
              title: Text('${r['department']}  (${r['employees']})'),
              subtitle: Text('Gross ${formatRupees('${r['gross']}')}  -  Net ${formatRupees('${r['net']}')}'),
              trailing: Text(formatRupees('${r['employer_cost']}')),
            ),
        ],
      ),
    );
  }

  Widget _trend(Map<String, dynamic> d) {
    final rows = ((d['trend'] as List?) ?? const []).cast<Map<String, dynamic>>();
    final maxP = rows.fold<int>(
      1,
      (m, r) => moneyToPaise('${r['gross']}') > m ? moneyToPaise('${r['gross']}') : m,
    );
    final small = Theme.of(context).textTheme.bodySmall;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('Last 12 months (gross)'),
            const SizedBox(height: 8),
            for (final r in rows)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 2),
                child: Row(
                  children: [
                    SizedBox(width: 64, child: Text('${r['period']}'.substring(2), style: small)),
                    Expanded(
                      child: LinearProgressIndicator(
                        value: moneyToPaise('${r['gross']}') / maxP,
                        minHeight: 10,
                      ),
                    ),
                    SizedBox(
                      width: 96,
                      child: Text(formatRupees('${r['gross']}'), textAlign: TextAlign.end, style: small),
                    ),
                  ],
                ),
              ),
          ],
        ),
      ),
    );
  }

  Widget _people(String title, Object? raw) {
    final list = ((raw as List?) ?? const []).cast<Map<String, dynamic>>();
    return Card(
      child: ExpansionTile(
        title: Text('$title (${list.length})'),
        children: [
          for (final p in list)
            ListTile(
              dense: true,
              title: Text('${p['name']}  -  ${p['employee_id']}'),
              trailing: Text('${p['date']}'),
            ),
        ],
      ),
    );
  }

  Widget _reportsCard(bool canBank) => Card(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Padding(padding: EdgeInsets.all(12), child: Text('Reports (Excel)')),
            for (final r in _reports)
              if (r.$1 != 'bank_advice' || canBank)
                ListTile(
                  key: ValueKey('report-${r.$1}'),
                  dense: true,
                  leading: const Icon(Icons.download_outlined),
                  title: Text(r.$2),
                  onTap: () => _download(r.$1),
                ),
          ],
        ),
      );
}

class _VarianceCard extends ConsumerWidget {
  const _VarianceCard({required this.period});
  final String period;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(payrollVarianceProvider(period));
    return Card(
      child: ExpansionTile(
        title: const Text('Changes since last month'),
        children: [
          async.when(
            loading: () => const Padding(padding: EdgeInsets.all(16), child: LinearProgressIndicator()),
            error: (e, _) => Padding(padding: const EdgeInsets.all(16), child: Text(payrollMsg(e))),
            data: (v) {
              final items = ((v['items'] as List?) ?? const []).cast<Map<String, dynamic>>();
              if (items.isEmpty) return const Padding(padding: EdgeInsets.all(16), child: Text('No changes.'));
              return Column(
                children: [
                  for (final i in items)
                    ListTile(
                      dense: true,
                      title: Text('${i['name']}  -  ${i['employee_id']}'),
                      subtitle: Text(
                        ((i['reasons'] as List?) ?? const []).map((r) => _reasons['$r'] ?? '$r').join(', '),
                      ),
                      trailing: Text('${formatRupees('${i['net_prev']}')} > ${formatRupees('${i['net']}')}'),
                    ),
                ],
              );
            },
          ),
        ],
      ),
    );
  }
}
