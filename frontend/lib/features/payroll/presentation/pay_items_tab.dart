import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../admin/presentation/admin_widgets.dart' show adminPermissionsProvider;
import '../domain/payroll_models.dart';
import 'arrears_section.dart';
import 'encashment_section.dart';
import 'loans_section.dart';
import 'one_time_section.dart';

enum PaySection {
  oneTime('One-time'),
  loans('Loans'),
  arrears('Arrears'),
  encashment('Encashment');

  const PaySection(this.label);
  final String label;
}

/// "Pay items" tab (plan 15, work package 3a): One-time | Loans | Arrears |
/// Encashment. Viewing needs `payroll.view_all`, writing `payroll.manage`
/// (the server enforces both again). The 3b segments (claims, overtime) are
/// not shown until they ship.
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
    return Column(
      children: [
        SizedBox(
          height: 52,
          child: ListView(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            children: [
              for (final s in PaySection.values)
                Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: ChoiceChip(
                    key: ValueKey('pay-section-${s.name}'),
                    label: Text(s.label),
                    selected: _section == s,
                    onSelected: (_) => setState(() => _section = s),
                  ),
                ),
            ],
          ),
        ),
        Expanded(
          child: switch (_section) {
            PaySection.oneTime => OneTimeSection(canManage: canManage),
            PaySection.loans => LoansSection(canManage: canManage),
            PaySection.arrears => ArrearsSection(canManage: canManage),
            PaySection.encashment => EncashmentSection(canManage: canManage),
          },
        ),
      ],
    );
  }
}
