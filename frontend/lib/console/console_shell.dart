import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/server/branding.dart';
import '../features/admin/domain/admin_models.dart' show AdminPerm;
import '../features/admin/presentation/audit_screen.dart';
import '../features/admin/presentation/company_screen.dart';
import '../features/admin/presentation/employees_screen.dart';
import '../features/admin/presentation/license_screen.dart';
import '../features/admin/presentation/reset_requests_screen.dart';
import '../features/admin/presentation/sessions_screen.dart';
import 'console_auth.dart';
import 'pages/attendance_day_page.dart';
import 'pages/dashboard_page.dart';

class ConsoleDestination {
  const ConsoleDestination(this.label, this.icon, this.permission, this.build);
  final String label;
  final IconData icon;

  /// Null = always shown. The server enforces every permission anyway.
  final String? permission;
  final Widget Function() build;
}

final List<ConsoleDestination> consoleDestinations = [
  ConsoleDestination('Dashboard', Icons.dashboard_outlined, null, () => const DashboardPage()),
  ConsoleDestination('Employees', Icons.people_outline, AdminPerm.employeeView,
      () => const EmployeesScreen(),),
  ConsoleDestination('Attendance today', Icons.fact_check_outlined, AdminPerm.attendanceViewAll,
      () => const AttendanceDayPage(),),
  ConsoleDestination('Password resets', Icons.lock_reset, AdminPerm.employeeResetPassword,
      () => const ResetRequestsScreen(),),
  ConsoleDestination('Sessions', Icons.devices_outlined, AdminPerm.sessionManage,
      () => const SessionsScreen(),),
  ConsoleDestination('Audit log', Icons.list_alt_outlined, AdminPerm.auditView,
      () => const AuditScreen(),),
  ConsoleDestination('License', Icons.verified_outlined, AdminPerm.licenseManage,
      () => const LicenseScreen(),),
  ConsoleDestination('Company', Icons.business_outlined, AdminPerm.companyManage,
      () => const CompanyScreen(),),
];

/// Destinations the user may open.
List<ConsoleDestination> visibleDestinations(List<String> permissions) => [
      for (final d in consoleDestinations)
        if (d.permission == null || permissions.contains(d.permission)) d,
    ];

class ConsoleShell extends ConsumerStatefulWidget {
  const ConsoleShell({super.key});

  @override
  ConsumerState<ConsoleShell> createState() => _ShellState();
}

class _ShellState extends ConsumerState<ConsoleShell> {
  int _index = 0;

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(consolePermissionsProvider);
    final items = visibleDestinations(perms);
    final index = _index.clamp(0, items.length - 1);
    final wide = MediaQuery.sizeOf(context).width >= 840;
    final user = ref.watch(consoleAuthProvider);
    final name = user is ConsoleSignedIn ? user.user.fullName : '';

    final nav = NavigationRail(
      extended: MediaQuery.sizeOf(context).width >= 1100,
      selectedIndex: index,
      onDestinationSelected: (i) => setState(() => _index = i),
      labelType: MediaQuery.sizeOf(context).width >= 1100 ? null : NavigationRailLabelType.all,
      destinations: [
        for (final d in items)
          NavigationRailDestination(icon: Icon(d.icon), label: Text(d.label)),
      ],
    );
    final body = KeyedSubtree(key: ValueKey(items[index].label), child: items[index].build());

    return Scaffold(
      appBar: AppBar(
        title: const BrandTitle(),
        actions: [
          if (name.isNotEmpty)
            Center(child: Padding(padding: const EdgeInsets.only(right: 8), child: Text(name))),
          TextButton.icon(
            onPressed: () => ref.read(consoleAuthProvider.notifier).logout(),
            icon: const Icon(Icons.logout),
            label: const Text('Sign out'),
          ),
          const SizedBox(width: 8),
        ],
      ),
      drawer: wide
          ? null
          : Drawer(
              child: ListView(
                children: [
                  for (var i = 0; i < items.length; i++)
                    ListTile(
                      leading: Icon(items[i].icon),
                      title: Text(items[i].label),
                      selected: i == index,
                      onTap: () {
                        setState(() => _index = i);
                        Navigator.pop(context);
                      },
                    ),
                ],
              ),
            ),
      body: wide
          ? Row(children: [nav, const VerticalDivider(width: 1), Expanded(child: body)])
          : body,
    );
  }
}
