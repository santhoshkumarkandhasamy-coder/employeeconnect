import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/security/screen_protect.dart';
import '../../../core/widgets/state_views.dart';
import '../../auth/presentation/auth_controller.dart';
import '../../payroll/presentation/payroll_home_screen.dart';
import '../domain/admin_models.dart';
import 'admin_providers.dart';
import 'admin_widgets.dart';
import 'audit_screen.dart';
import 'company_screen.dart';
import 'employees_screen.dart';
import 'license_screen.dart';
import 'master_screen.dart';
import 'reset_requests_screen.dart';
import 'roles_screen.dart';
import 'sessions_screen.dart';
import 'settings_screen.dart';

IconData _icon(AdminSection s) => switch (s) {
      AdminSection.employees => Icons.people_outline,
      AdminSection.resetRequests => Icons.lock_reset,
      AdminSection.roles => Icons.admin_panel_settings_outlined,
      AdminSection.departments => Icons.apartment_outlined,
      AdminSection.shifts => Icons.schedule,
      AdminSection.holidays => Icons.event_outlined,
      AdminSection.leaveTypes => Icons.beach_access_outlined,
      AdminSection.clientLocations => Icons.business_center_outlined,
      AdminSection.settings => Icons.settings_outlined,
      AdminSection.audit => Icons.fact_check_outlined,
      AdminSection.sessions => Icons.devices_outlined,
      AdminSection.license => Icons.verified_outlined,
      AdminSection.company => Icons.business_outlined,
      AdminSection.payroll => Icons.payments_outlined,
    };

Widget _screenFor(AdminSection s) => switch (s) {
      AdminSection.employees => const EmployeesScreen(),
      AdminSection.resetRequests => const ResetRequestsScreen(),
      AdminSection.roles => const RolesScreen(),
      AdminSection.departments => const MasterScreen(spec: departmentSpec),
      AdminSection.shifts => MasterScreen(spec: shiftSpec),
      AdminSection.holidays => MasterScreen(spec: holidaySpec),
      AdminSection.leaveTypes => MasterScreen(spec: leaveTypeSpec),
      AdminSection.clientLocations => MasterScreen(spec: clientLocationSpec),
      AdminSection.settings => const SettingsScreen(),
      AdminSection.audit => const AuditScreen(),
      AdminSection.sessions => const SessionsScreen(),
      AdminSection.license => const LicenseScreen(),
      AdminSection.company => const CompanyScreen(),
      AdminSection.payroll => const PayrollHomeScreen(),
    };

/// Admin home: one tile per section the user is permitted to open. The
/// signed-in user's permissions are re-fetched (`/auth/me`) when it opens so a
/// role edited since sign-in is reflected (L9).
class AdminHomeScreen extends ConsumerStatefulWidget {
  const AdminHomeScreen({super.key});

  @override
  ConsumerState<AdminHomeScreen> createState() => _AdminHomeScreenState();
}

class _AdminHomeScreenState extends ConsumerState<AdminHomeScreen> {
  @override
  void initState() {
    super.initState();
    Future<void>.microtask(() async {
      if (mounted) await ref.read(authControllerProvider.notifier).refreshUser();
    });
  }

  @override
  Widget build(BuildContext context) {
    final sections = adminSections(ref.watch(adminPermissionsProvider));
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Admin')),
        body: sections.isEmpty
            ? const EmptyView(
                icon: Icons.lock_outline,
                message: 'You do not have access to any admin tools.',
              )
            : ListView(
                children: [
                  if (ref.watch(adminPermissionsProvider).contains(AdminPerm.licenseManage))
                    if (ref.watch(licenseProvider).valueOrNull case final l?)
                      LicenseBanner(license: l),
                  for (final s in sections)
                    ListTile(
                      leading: Icon(_icon(s)),
                      title: Text(s.label),
                      trailing: const Icon(Icons.chevron_right),
                      onTap: () => Navigator.of(context).push(
                        MaterialPageRoute<void>(builder: (_) => _screenFor(s)),
                      ),
                    ),
                ],
              ),
      ),
    );
  }
}
