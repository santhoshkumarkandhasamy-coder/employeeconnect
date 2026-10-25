import 'package:flutter/material.dart';

import '../../../core/utils/dates.dart';
import '../domain/payroll_models.dart';
import '../domain/payroll_money.dart';
import 'payroll_widgets.dart';

/// Read-only payslip body shared by the employee detail and the admin views.
/// Amounts come from the server as strings and are only formatted here.
class PayslipView extends StatelessWidget {
  const PayslipView({required this.payslip, super.key, this.showMonthly = true});
  final Payslip payslip;
  final bool showMonthly;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final p = payslip;
    final e = p.employee;
    final totals = p.totals;
    final join = parseYmd(e.joinDate);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Semantics(
          header: true,
          child: Text(
            p.companyName.isEmpty ? 'Payslip' : p.companyName,
            style: theme.textTheme.titleLarge,
          ),
        ),
        if (p.companyAddress != null) Text(p.companyAddress!, style: theme.textTheme.bodySmall),
        const SizedBox(height: 4),
        Text(
          'Payslip for ${periodTitle(p.period)}'
          '${p.revision > 1 ? ' (revision ${p.revision})' : ''}',
          style: theme.textTheme.titleMedium,
        ),
        SectionCard(
          title: 'Employee',
          children: [
            _kv(context, 'Name', e.fullName),
            _kv(context, 'Employee ID', e.employeeId),
            if (e.department != null) _kv(context, 'Department', e.department!),
            if (e.designation != null) _kv(context, 'Designation', e.designation!),
            if (join != null) _kv(context, 'Date of joining', formatDayLong(join)),
            if (e.pan != null) _kv(context, 'PAN', e.pan!),
            if (e.uan != null) _kv(context, 'UAN', e.uan!),
            if (e.esiIpNumber != null) _kv(context, 'ESI number', e.esiIpNumber!),
            if (e.bankName != null) _kv(context, 'Bank', e.bankName!),
            if (e.bankAccount != null) _kv(context, 'Account', e.bankAccount!),
            _kv(context, 'Payment mode', _mode(e.paymentMode)),
          ],
        ),
        SectionCard(
          title: 'Days',
          children: [
            _kv(context, 'Days in basis', formatDays(p.days.basisDays)),
            _kv(context, 'Loss of pay', formatDays(p.days.lopDays)),
            _kv(context, 'Paid days', formatDays(p.days.paidDays)),
          ],
        ),
        if (p.earnings.isNotEmpty)
          SectionCard(
            title: 'Earnings',
            children: [
              ...payslipLineRows(p.earnings, showMonthly: showMonthly),
              if (totals != null) ...[
                const Divider(),
                AmountRow(label: 'Gross earnings', value: formatRupees(totals.grossEarnings), bold: true),
              ],
            ],
          ),
        if (p.deductions.isNotEmpty)
          SectionCard(
            title: 'Deductions',
            children: [
              ...payslipLineRows(p.deductions),
              if (totals != null) ...[
                const Divider(),
                AmountRow(
                  label: 'Total deductions',
                  value: formatRupees(totals.totalDeductions),
                  bold: true,
                ),
              ],
            ],
          ),
        if (p.employerContributions != null && p.employerContributions!.isNotEmpty)
          SectionCard(
            title: 'Employer contributions',
            children: payslipLineRows(p.employerContributions!),
          ),
        if (totals != null)
          Card(
            color: theme.colorScheme.primaryContainer,
            margin: const EdgeInsets.symmetric(vertical: 6),
            child: Padding(
              padding: const EdgeInsets.all(14),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  AmountRow(label: 'Net pay', value: formatRupees(totals.netPay), bold: true),
                  if (totals.netPayWords != null)
                    Text(totals.netPayWords!, style: theme.textTheme.bodySmall),
                ],
              ),
            ),
          ),
      ],
    );
  }

  static String _mode(String m) => switch (m) {
        'bank_transfer' => 'Bank transfer',
        'cheque' => 'Cheque',
        'cash' => 'Cash',
        _ => m,
      };

  Widget _kv(BuildContext context, String k, String v) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
              width: 120,
              child: Text(k, style: Theme.of(context).textTheme.bodySmall),
            ),
            Expanded(child: Text(v)),
          ],
        ),
      );
}
