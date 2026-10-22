import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../admin/domain/admin_models.dart' show canOpenAdmin;
import '../admin/presentation/admin_home_screen.dart';
import '../announcements/domain/announcement_models.dart';
import '../announcements/presentation/admin_announcements_screen.dart';
import '../announcements/presentation/announcement_providers.dart';
import '../announcements/presentation/announcements_screen.dart';
import '../auth/presentation/auth_controller.dart';
import '../auth/presentation/auth_state.dart';
import '../export/domain/export_models.dart' show canOpenExport;
import '../export/presentation/export_screen.dart';
import '../food/domain/food_models.dart';
import '../food/presentation/food_report_screen.dart';
import '../late/presentation/late_screen.dart';
import '../leave/domain/leave_logic.dart';
import '../leave/presentation/approvals_screen.dart';
import '../placeholders/placeholder_screen.dart';
import '../profile/presentation/profile_screen.dart';

/// More: later features (Late, Announcements, Profile, Admin) plus
/// account actions (biometrics, logout).
class MoreScreen extends ConsumerWidget {
  const MoreScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final user = ref.watch(
      authControllerProvider.select((s) => s is AuthAuthenticated ? s.user : null),
    );
    final bio = ref.watch(
      authControllerProvider.select((s) => s is AuthAuthenticated && s.biometricEnabled),
    );
    final ctl = ref.read(authControllerProvider.notifier);
    // Shown only when at least one admin tool is permitted (not by role name).
    final showAdmin = user != null && canOpenAdmin(user.permissions);

    Future<void> confirmLogout({required bool all}) async {
      final ok = await showDialog<bool>(
        context: context,
        builder: (ctx) => AlertDialog(
          title: Text(all ? 'Log out of all devices?' : 'Log out?'),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(ctx, false),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(ctx, true),
              style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
              child: const Text('Log out'),
            ),
          ],
        ),
      );
      if (!(ok ?? false) || !context.mounted) return;
      await ctl.logout(all: all);
    }

    return Scaffold(
      appBar: AppBar(title: const Text('More')),
      body: ListView(
        children: [
          if (user != null)
            ListTile(
              leading: const CircleAvatar(child: Icon(Icons.person)),
              title: Text(user.fullName.isEmpty ? user.employeeId : user.fullName),
              subtitle: Text('${user.employeeId} - ${user.role}'),
            ),
          const Divider(),
          _navTile(
            context,
            'Late records',
            Icons.schedule,
            builder: (_) => const LateScreen(),
          ),
          if (user != null && canOpenApprovals(user.permissions))
            _navTile(
              context,
              'Approvals',
              Icons.fact_check_outlined,
              builder: (_) => const ApprovalsScreen(),
            ),
          if (user != null && canViewFoodReport(user.permissions))
            _navTile(
              context,
              'Food report',
              Icons.restaurant_menu_outlined,
              builder: (_) => const FoodReportScreen(),
            ),
          _navTile(
            context,
            'Announcements',
            Icons.campaign_outlined,
            builder: (_) => const AnnouncementsScreen(),
            badge: badgeText(ref.watch(unreadCountProvider).valueOrNull ?? 0),
          ),
          if (user != null && canManageAnnouncements(user.permissions))
            _navTile(
              context,
              'Manage announcements',
              Icons.edit_notifications_outlined,
              builder: (_) => const AdminAnnouncementsScreen(),
            ),
          _navTile(
            context,
            'Profile',
            Icons.person_outline,
            builder: (_) => const ProfileScreen(),
          ),
          if (user != null && canOpenExport(user.permissions))
            _navTile(
              context,
              'Reports & Export',
              Icons.file_download_outlined,
              builder: (_) => const ExportScreen(),
            ),
          if (showAdmin)
            _navTile(
              context,
              'Admin',
              Icons.admin_panel_settings_outlined,
              builder: (_) => const AdminHomeScreen(),
            ),
          const Divider(),
          SwitchListTile(
            secondary: const Icon(Icons.fingerprint),
            title: const Text('Use biometrics'),
            subtitle: const Text('Unlock the app with your fingerprint or face'),
            value: bio,
            onChanged: (v) async {
              final ok = await ctl.setBiometric(enabled: v);
              if (!ok && v && context.mounted) {
                ScaffoldMessenger.of(context).showSnackBar(
                  const SnackBar(content: Text('Biometrics are not available.')),
                );
              }
            },
          ),
          ListTile(
            leading: const Icon(Icons.logout),
            title: const Text('Log out'),
            onTap: () => confirmLogout(all: false),
          ),
          ListTile(
            leading: const Icon(Icons.devices_other_outlined),
            title: const Text('Log out of all devices'),
            onTap: () => confirmLogout(all: true),
          ),
        ],
      ),
    );
  }

  Widget _navTile(
    BuildContext context,
    String label,
    IconData icon, {
    WidgetBuilder? builder,
    String? badge,
  }) =>
      ListTile(
        leading: Icon(icon),
        title: Text(label),
        subtitle: badge == null ? null : Text('$badge unread'),
        trailing: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (badge != null) Badge(label: Text(badge)),
            const Icon(Icons.chevron_right),
          ],
        ),
        onTap: () => Navigator.of(context).push(
          MaterialPageRoute<void>(
            builder: builder ?? (_) => PlaceholderScreen(title: label),
          ),
        ),
      );
}
