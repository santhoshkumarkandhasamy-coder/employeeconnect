import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../features/announcements/domain/announcement_models.dart' show badgeText;
import '../features/announcements/presentation/announcement_providers.dart';
import '../features/auth/presentation/auth_controller.dart';

/// Bottom navigation shell: Home, Attendance, Leave, Food, More.
class AppShell extends ConsumerWidget {
  const AppShell({required this.shell, super.key});
  final StatefulNavigationShell shell;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final compromised = ref.watch(deviceCompromisedProvider);
    final unread = badgeText(ref.watch(unreadCountProvider).valueOrNull ?? 0);
    return Scaffold(
      body: Column(
        children: [
          if (compromised)
            const MaterialBanner(
              content: Text(
                'This device appears to be rooted or jailbroken. '
                'Your data may be at risk.',
              ),
              leading: Icon(Icons.warning_amber_rounded),
              actions: [SizedBox.shrink()],
            ),
          Expanded(child: shell),
        ],
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: shell.currentIndex,
        onDestinationSelected: (i) =>
            shell.goBranch(i, initialLocation: i == shell.currentIndex),
        destinations: [
          const NavigationDestination(
            icon: Icon(Icons.home_outlined),
            selectedIcon: Icon(Icons.home),
            label: 'Home',
          ),
          const NavigationDestination(
            icon: Icon(Icons.fingerprint_outlined),
            selectedIcon: Icon(Icons.fingerprint),
            label: 'Attendance',
          ),
          const NavigationDestination(
            icon: Icon(Icons.event_note_outlined),
            selectedIcon: Icon(Icons.event_note),
            label: 'Leave',
          ),
          const NavigationDestination(
            icon: Icon(Icons.restaurant_outlined),
            selectedIcon: Icon(Icons.restaurant),
            label: 'Food',
          ),
          NavigationDestination(
            icon: Badge(
              isLabelVisible: unread != null,
              label: Text(unread ?? ''),
              child: const Icon(Icons.menu),
            ),
            selectedIcon: Badge(
              isLabelVisible: unread != null,
              label: Text(unread ?? ''),
              child: const Icon(Icons.menu_open),
            ),
            label: 'More',
            tooltip: unread == null ? 'More' : 'More, $unread unread announcements',
          ),
        ],
      ),
    );
  }
}
