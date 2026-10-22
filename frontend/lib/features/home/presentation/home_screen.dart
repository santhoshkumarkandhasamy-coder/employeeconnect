import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../app/router.dart' show Routes;
import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/server/branding.dart';
import '../../attendance/presentation/attendance_providers.dart';
import '../../attendance/presentation/widgets/summary_card.dart' show StaleBanner;
import '../../attendance/presentation/widgets/today_status_card.dart';
import '../../announcements/domain/announcement_models.dart';
import '../../announcements/presentation/announcement_detail_screen.dart';
import '../../announcements/presentation/announcement_providers.dart';
import '../../announcements/presentation/announcements_screen.dart';
import '../../auth/presentation/auth_controller.dart';
import '../../auth/presentation/auth_state.dart';
import '../../profile/presentation/profile_screen.dart';
import '../data/home_repository.dart';
import '../domain/home_models.dart';
import 'home_widgets.dart';

/// Home dashboard: today's status, quick actions, food toggle, balances,
/// latest announcements and celebrations. Each part tolerates being missing
/// (the server nulls a part that failed); the last good response is cached.
class HomeScreen extends ConsumerWidget {
  const HomeScreen({super.key, this.clock});

  /// Test hook.
  final DateTime Function()? clock;

  Future<void> _refresh(WidgetRef ref) async {
    ref
      ..invalidate(todayAttendanceProvider)
      ..invalidate(latestAnnouncementsProvider)
      ..invalidate(unreadCountProvider)
      ..invalidate(homeProvider);
    await ref.read(homeProvider.future).then((_) {}, onError: (Object _) {});
  }

  void _go(BuildContext context, String route) => GoRouter.maybeOf(context)?.go(route);

  void _push(BuildContext context, Widget screen) => Navigator.of(context).push(
        MaterialPageRoute<void>(builder: (_) => screen),
      );

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final now = (clock ?? DateTime.now)();
    final name = ref.watch(authControllerProvider.select(
      (s) => s is AuthAuthenticated ? s.user.fullName : '',
    ),);
    final first = name.trim().isEmpty ? '' : name.trim().split(RegExp(r'\s+')).first;
    final async = ref.watch(homeProvider);
    final unread = ref.watch(unreadCountProvider).valueOrNull;

    final actions = [
      QuickAction('Attendance', Icons.fingerprint, () => _go(context, Routes.attendance)),
      QuickAction('Apply leave', Icons.event_available_outlined, () => _go(context, Routes.leave)),
      QuickAction('Food', Icons.restaurant_outlined, () => _go(context, Routes.food)),
      QuickAction('Announcements', Icons.campaign_outlined,
          () => _push(context, const AnnouncementsScreen()),),
      QuickAction('Profile', Icons.person_outline, () => _push(context, const ProfileScreen())),
    ];

    Widget body(HomeData? d, {required bool stale, Object? error}) {
      return ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 24),
        children: [
          if (stale) const StaleBanner(),
          TodayStatusCard(
            onTap: () => _go(context, Routes.attendance),
            fallbackSubtitle: d?.attendance?.text,
          ),
          const SizedBox(height: 8),
          QuickActionsRow(actions: actions),
          const SizedBox(height: 8),
          if (d == null)
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Row(
                  children: [
                    const Icon(Icons.cloud_off_outlined),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Text(error is ApiException ? error.message : 'Could not load your dashboard.'),
                    ),
                    TextButton(onPressed: () => _refresh(ref), child: const Text('Try again')),
                  ],
                ),
              ),
            )
          else ...[
            if (d.food != null) FoodTodayCard(food: d.food!, offline: stale),
            BalanceSummaryCard(data: d, onTap: () => _go(context, Routes.leave)),
            AnnouncementsPreviewCard(
              unread: unread ?? d.unreadAnnouncements,
              onOpenAll: () => _push(context, const AnnouncementsScreen()),
              onOpen: (Announcement a) =>
                  _push(context, AnnouncementDetailScreen(id: a.id, initial: a)),
            ),
            CelebrationsCard(items: d.celebrations, now: now),
          ],
        ],
      );
    }

    return SecureScreen(
        child: Scaffold(
      appBar: AppBar(
        title: Text(first.isEmpty ? greeting(now) : '${greeting(now)}, $first'),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 12),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 130),
              child: const Center(child: BrandTitle()),
            ),
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () => _refresh(ref),
        child: async.when(
          skipLoadingOnReload: true,
          loading: () => const SingleChildScrollView(
            physics: AlwaysScrollableScrollPhysics(),
            padding: EdgeInsets.fromLTRB(12, 8, 12, 24),
            child: HomeSkeleton(),
          ),
          error: (e, _) => body(null, stale: false, error: e),
          data: (c) => body(c.value, stale: c.stale),
        ),
      ),
    ),);
  }
}
