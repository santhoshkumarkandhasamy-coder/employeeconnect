import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/security/screen_protect.dart';
import '../../admin/presentation/admin_widgets.dart' show NoPermissionScreen, adminPermissionsProvider;
import '../domain/payroll_models.dart';
import 'employees_tab.dart';
import 'payroll_settings_tab.dart';
import 'runs_tab.dart';

/// HR / Admin payroll area: Runs | Employees | Settings. Shown with
/// `payroll.view_all`; write actions need `payroll.manage` / `payroll.publish`
/// (the server enforces all of it again). FLAG_SECURE is on for every payroll
/// admin screen.
class PayrollHomeScreen extends ConsumerWidget {
  const PayrollHomeScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!canOpenPayroll(perms)) return const NoPermissionScreen(title: 'Payroll');
    return SecureScreen(
      child: DefaultTabController(
        length: 3,
        child: Scaffold(
          appBar: AppBar(
            title: const Text('Payroll'),
            bottom: const TabBar(
              tabs: [
                Tab(text: 'Runs'),
                Tab(text: 'Employees'),
                Tab(text: 'Settings'),
              ],
            ),
          ),
          body: const TabBarView(
            children: [RunsTab(), PayrollEmployeesTab(), PayrollSettingsTab()],
          ),
        ),
      ),
    );
  }
}
