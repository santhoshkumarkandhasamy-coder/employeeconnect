import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/security/screen_protect.dart';
import '../../admin/presentation/admin_widgets.dart' show NoPermissionScreen, adminPermissionsProvider;
import '../domain/payroll_models.dart';
import '../domain/phase4a_models.dart' show canApprovePayroll;
import 'approvals_tab.dart';
import 'dashboard_tab.dart';
import 'employees_tab.dart';
import 'pay_items_tab.dart';
import 'payments_tab.dart';
import 'payroll_providers.dart' show approvalQueueProvider;
import 'payroll_settings_tab.dart';
import 'runs_tab.dart';
import 'statutory_tab.dart';

/// HR / Admin payroll area: Dashboard | Runs | Employees | Pay items | Approvals (only with
/// `payroll.approve` or `payroll.publish`) | Payments | Statutory | Settings. Shown with
/// `payroll.view_all`; write actions need `payroll.manage` / `payroll.publish`
/// (the server enforces all of it again). FLAG_SECURE is on for every payroll
/// admin screen.
class PayrollHomeScreen extends ConsumerWidget {
  const PayrollHomeScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!canOpenPayroll(perms)) return const NoPermissionScreen(title: 'Payroll');
    // Approvals: payment batches, increments, F&F (approve) and publish requests (publish).
    final approver = canApprovePayroll(perms) || canPublishPayroll(perms);
    final pending = approver ? ref.watch(approvalQueueProvider).valueOrNull?.count ?? 0 : 0;
    return SecureScreen(
      child: DefaultTabController(
        length: approver ? 8 : 7,
        child: Scaffold(
          appBar: AppBar(
            title: const Text('Payroll'),
            bottom: TabBar(
              isScrollable: true,
              tabAlignment: TabAlignment.start,
              tabs: [
                const Tab(text: 'Dashboard'),
                const Tab(text: 'Runs'),
                const Tab(text: 'Employees'),
                const Tab(text: 'Pay items'),
                if (approver)
                  Tab(
                    key: const ValueKey('tab-approvals'),
                    child: Badge(
                      label: Text('$pending'),
                      isLabelVisible: pending > 0,
                      offset: const Offset(10, -6),
                      child: const Text('Approvals'),
                    ),
                  ),
                const Tab(key: ValueKey('tab-payments'), text: 'Payments'),
                const Tab(text: 'Statutory'),
                const Tab(text: 'Settings'),
              ],
            ),
          ),
          body: TabBarView(
            children: [
              const PayrollDashboardTab(),
              const RunsTab(),
              const PayrollEmployeesTab(),
              const PayItemsTab(),
              if (approver) const ApprovalsTab(),
              const PaymentsTab(),
              const StatutoryTab(),
              const PayrollSettingsTab(),
            ],
          ),
        ),
      ),
    );
  }
}
