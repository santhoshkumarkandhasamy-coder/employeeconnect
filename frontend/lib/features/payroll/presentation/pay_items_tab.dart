import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../admin/presentation/admin_widgets.dart' show adminPermissionsProvider;
import '../domain/payroll_models.dart';
import 'arrears_section.dart';
import 'claims_review.dart';
import 'encashment_section.dart';
import 'exits_section.dart';
import 'increments_section.dart';
import 'loans_section.dart';
import 'one_time_section.dart';
import 'overtime_section.dart';
import 'payroll_providers.dart';

enum PaySection {
  oneTime('One-time'),
  loans('Loans'),
  arrears('Arrears'),
  encashment('Encashment'),
  claims('Claims'),
  overtime('Overtime'),
  increments('Increments'),
  exits('Exits');

  const PaySection(this.label);
  final String label;
}

/// "Pay items" tab (plan 15, work package 3a): One-time | Loans | Arrears |
/// Encashment. Viewing needs `payroll.view_all`, writing `payroll.manage`
/// (the server enforces both again). 3b adds Claims (HR queue) and Overtime,
/// which is shown only while `payroll.overtime_enabled` is on.
class PayItemsTab extends ConsumerStatefulWidget {
  const PayItemsTab({super.key});

  @override
  ConsumerState<PayItemsTab> createState() => _PayItemsTabState();
}

class _PayItemsTabState extends ConsumerState<PayItemsTab> {
  PaySection _section = PaySection.oneTime;

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    final canManage = canManagePayroll(perms);
    final overtimeOn = ref.watch(overtimeEnabledProvider).valueOrNull ?? false;
    final sections = [for (final s in PaySection.values) if (s != PaySection.overtime || overtimeOn) s];
    final section = sections.contains(_section) ? _section : PaySection.oneTime;
    return Column(
      children: [
        SizedBox(
          height: 52,
          child: ListView(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            children: [
              for (final s in sections)
                Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: ChoiceChip(
                    key: ValueKey('pay-section-${s.name}'),
                    label: Text(s.label),
                    selected: section == s,
                    onSelected: (_) => setState(() => _section = s),
                  ),
                ),
            ],
          ),
        ),
        Expanded(
          child: switch (section) {
            PaySection.oneTime => OneTimeSection(canManage: canManage),
            PaySection.loans => LoansSection(canManage: canManage),
            PaySection.arrears => ArrearsSection(canManage: canManage),
            PaySection.encashment => EncashmentSection(canManage: canManage),
            PaySection.claims => ClaimsSection(canManage: canManage),
            PaySection.overtime => OvertimeSection(canManage: canManage),
            PaySection.increments => IncrementsSection(canManage: canManage),
            PaySection.exits => ExitsSection(canManage: canManage),
          },
        ),
      ],
    );
  }
}
