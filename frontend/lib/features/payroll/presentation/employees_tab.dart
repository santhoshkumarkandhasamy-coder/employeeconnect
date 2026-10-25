import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../admin/presentation/admin_widgets.dart' show AdminPagedBody;
import '../domain/payroll_models.dart';
import '../domain/payroll_money.dart';
import 'employee_payroll_screen.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// Employees with their payroll status: structure, gross and missing details.
class PayrollEmployeesTab extends ConsumerStatefulWidget {
  const PayrollEmployeesTab({super.key});

  @override
  ConsumerState<PayrollEmployeesTab> createState() => _PayrollEmployeesTabState();
}

class _PayrollEmployeesTabState extends ConsumerState<PayrollEmployeesTab> {
  final _search = TextEditingController();
  Timer? _debounce;
  PayrollEmployeeQuery _query = const PayrollEmployeeQuery();

  @override
  void dispose() {
    _debounce?.cancel();
    _search.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(payrollEmployeesProvider(_query));
    final notifier = ref.read(payrollEmployeesProvider(_query).notifier);
    return AdminPagedBody<PayrollEmployee>(
      async: async,
      onRefresh: notifier.reload,
      onLoadMore: notifier.loadMore,
      emptyIcon: Icons.people_outline,
      emptyMessage: _query.q.isNotEmpty || _query.hasStructure != null
          ? 'No employees match these filters.'
          : 'No employees yet.',
      header: Padding(
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 0),
        child: Column(
          children: [
            TextField(
              controller: _search,
              textInputAction: TextInputAction.search,
              decoration: const InputDecoration(
                labelText: 'Search name or ID',
                prefixIcon: Icon(Icons.search),
                border: OutlineInputBorder(),
                isDense: true,
              ),
              onChanged: (v) {
                _debounce?.cancel();
                _debounce = Timer(const Duration(milliseconds: 400), () {
                  if (mounted) {
                    setState(() => _query = PayrollEmployeeQuery(q: v, hasStructure: _query.hasStructure));
                  }
                });
              },
            ),
            const SizedBox(height: 8),
            SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              child: Row(
                children: [
                  for (final f in const <(bool?, String)>[
                    (null, 'All'),
                    (true, 'Has structure'),
                    (false, 'No structure'),
                  ])
                    Padding(
                      padding: const EdgeInsets.only(right: 8),
                      child: ChoiceChip(
                        label: Text(f.$2),
                        selected: _query.hasStructure == f.$1,
                        onSelected: (_) => setState(
                          () => _query = PayrollEmployeeQuery(q: _query.q, hasStructure: f.$1),
                        ),
                      ),
                    ),
                ],
              ),
            ),
          ],
        ),
      ),
      itemBuilder: (context, e) => PayrollEmployeeTile(
        employee: e,
        onTap: () async {
          await Navigator.of(context).push<void>(
            MaterialPageRoute<void>(builder: (_) => EmployeePayrollScreen(employee: e.user)),
          );
          ref.invalidate(payrollEmployeesProvider(_query));
        },
      ),
    );
  }
}

class PayrollEmployeeTile extends StatelessWidget {
  const PayrollEmployeeTile({required this.employee, required this.onTap, super.key});
  final PayrollEmployee employee;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final e = employee;
    final s = e.currentStructure;
    return Card(
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: ListTile(
        title: Text(
          e.user.fullName.isEmpty ? e.user.employeeId : e.user.fullName,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
        ),
        subtitle: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              [
                e.user.employeeId,
                if (e.department != null) e.department!,
                if (!e.isActive) 'Inactive',
                if (!e.includeInPayroll) 'Not in payroll',
              ].join('  -  '),
            ),
            Text(
              s == null
                  ? 'No salary structure'
                  : 'Gross ${formatRupees(s.monthlyGross)} from ${s.effectiveFrom}',
            ),
            if (e.flags.isNotEmpty) FlagChips(warnings: e.flags),
          ],
        ),
        isThreeLine: true,
        trailing: const Icon(Icons.chevron_right),
        onTap: onTap,
      ),
    );
  }
}
