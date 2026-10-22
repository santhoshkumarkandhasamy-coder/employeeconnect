import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/files/file_service.dart' show UserAvatar;
import '../../../core/widgets/skeleton.dart';
import '../../announcements/domain/announcement_models.dart';
import '../../announcements/presentation/announcement_providers.dart';
import '../../food/data/food_repository.dart';
import '../../food/domain/food_models.dart';
import '../../food/presentation/food_providers.dart';
import '../domain/home_models.dart';

/// Card with a bold title and a body.
class HomeCard extends StatelessWidget {
  const HomeCard({required this.title, required this.child, super.key, this.trailing, this.icon});
  final String title;
  final Widget child;
  final Widget? trailing;
  final IconData? icon;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                if (icon != null) ...[
                  Icon(icon, size: 20, color: theme.colorScheme.primary),
                  const SizedBox(width: 8),
                ],
                Expanded(
                  child: Semantics(
                    header: true,
                    child: Text(title, style: theme.textTheme.titleMedium),
                  ),
                ),
                if (trailing != null) trailing!,
              ],
            ),
            const SizedBox(height: 8),
            child,
          ],
        ),
      ),
    );
  }
}

/// Placeholder layout shown while `/home` loads.
class HomeSkeleton extends StatelessWidget {
  const HomeSkeleton({super.key});

  @override
  Widget build(BuildContext context) => const Column(
        children: [
          SkeletonCard(label: 'Loading your dashboard'),
          SkeletonCard(lines: 3),
          SkeletonCard(),
          SkeletonCard(lines: 3),
        ],
      );
}

class QuickAction {
  const QuickAction(this.label, this.icon, this.onTap);
  final String label;
  final IconData icon;
  final VoidCallback onTap;
}

class QuickActionsRow extends StatelessWidget {
  const QuickActionsRow({required this.actions, super.key});
  final List<QuickAction> actions;

  @override
  Widget build(BuildContext context) => Wrap(
        spacing: 8,
        runSpacing: 8,
        children: [
          for (final a in actions)
            ActionChip(
              avatar: Icon(a.icon, size: 18),
              label: Text(a.label),
              onPressed: a.onTap,
              materialTapTargetSize: MaterialTapTargetSize.padded,
            ),
        ],
      );
}

/// Leave and permission balances plus the pending-requests count.
class BalanceSummaryCard extends StatelessWidget {
  const BalanceSummaryCard({required this.data, required this.onTap, super.key});
  final HomeData data;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final bal = data.leaveBalances;
    final perm = data.permission;
    final pending = data.pendingRequests;
    return HomeCard(
      title: 'Leave and permission',
      icon: Icons.event_note_outlined,
      trailing: TextButton(onPressed: onTap, child: const Text('Open')),
      child: bal == null && perm == null
          ? const Text('Balances are not available right now.')
          : Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (bal != null)
                  bal.isEmpty
                      ? const Text('No leave types are set up for you yet.')
                      : Wrap(
                          spacing: 8,
                          runSpacing: 4,
                          children: [
                            for (final b in bal)
                              Chip(
                                label: Text('${b.type}: ${formatBalance(b.remaining)}'),
                                visualDensity: VisualDensity.compact,
                              ),
                          ],
                        ),
                if (perm != null) ...[
                  const SizedBox(height: 8),
                  Text('Permission this month: ${perm.label}', style: theme.textTheme.bodyMedium),
                ],
                if (pending != null && pending > 0) ...[
                  const SizedBox(height: 8),
                  Row(
                    children: [
                      Icon(Icons.hourglass_top, size: 16, color: theme.colorScheme.tertiary),
                      const SizedBox(width: 6),
                      Text('$pending request${pending == 1 ? '' : 's'} waiting for approval'),
                    ],
                  ),
                ],
              ],
            ),
    );
  }
}

/// `8`, `7.5` (days left).
String formatBalance(double d) => d == d.roundToDouble() ? '${d.round()}' : d.toStringAsFixed(1);

/// "Need food today?" quick switch. Uses `food_today` from `/home`; the
/// change goes through the same endpoint as the Food tab and the server's
/// cut-off decides (`can_change`).
class FoodTodayCard extends ConsumerStatefulWidget {
  const FoodTodayCard({required this.food, required this.offline, super.key});
  final HomeFood food;
  final bool offline;

  @override
  ConsumerState<FoodTodayCard> createState() => _FoodTodayCardState();
}

class _FoodTodayCardState extends ConsumerState<FoodTodayCard> {
  late bool? _value = widget.food.needFood;
  late bool _canChange = widget.food.canChange;
  bool _busy = false;

  @override
  void didUpdateWidget(FoodTodayCard old) {
    super.didUpdateWidget(old);
    if (old.food != widget.food && !_busy) {
      _value = widget.food.needFood;
      _canChange = widget.food.canChange;
    }
  }

  Future<void> _toggle(bool v) async {
    final before = _value;
    final messenger = ScaffoldMessenger.of(context);
    setState(() {
      _value = v;
      _busy = true;
    });
    try {
      final saved = await ref.read(foodRepositoryProvider).setOne(widget.food.date, needFood: v);
      ref.invalidate(foodProvider);
      if (!mounted) return;
      setState(() {
        _value = saved.needFood ?? v;
        _canChange = saved.canChange;
        _busy = false;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _value = before;
        _busy = false;
        if (e.code == 'FOOD_CUTOFF_PASSED') _canChange = false;
      });
      messenger.showSnackBar(SnackBar(content: Text(foodErrorMessage(e))));
    }
  }

  @override
  Widget build(BuildContext context) {
    final enabled = _canChange && !_busy && !widget.offline;
    final answer = _value == true ? 'Yes' : (_value == false ? 'No' : 'Not answered');
    final note = !_canChange
        ? 'Cut-off has passed for today.'
        : (widget.offline ? 'Offline - connect to change.' : 'Answer: $answer');
    return HomeCard(
      title: 'Need food today?',
      icon: Icons.restaurant_outlined,
      child: Semantics(
        container: true,
        label: 'Need food today: $answer. $note',
        child: Row(
          children: [
            Expanded(child: ExcludeSemantics(child: Text(note))),
            Switch(
              key: const ValueKey('home-food-switch'),
              value: _value ?? false,
              onChanged: enabled ? _toggle : null,
            ),
          ],
        ),
      ),
    );
  }
}

/// Unread count plus the latest announcements.
class AnnouncementsPreviewCard extends ConsumerWidget {
  const AnnouncementsPreviewCard({
    required this.unread,
    required this.onOpenAll,
    required this.onOpen,
    super.key,
  });
  final int? unread;
  final VoidCallback onOpenAll;
  final void Function(Announcement) onOpen;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final latest = ref.watch(latestAnnouncementsProvider);
    final theme = Theme.of(context);
    final unreadLabel = unread == null || unread == 0 ? null : '$unread unread';
    return HomeCard(
      title: 'Announcements',
      icon: Icons.campaign_outlined,
      trailing: TextButton(
        onPressed: onOpenAll,
        child: Text(unreadLabel == null ? 'View all' : 'View all ($unreadLabel)'),
      ),
      child: latest.when(
        loading: () => const Column(
          children: [SkeletonBox(), SizedBox(height: 8), SkeletonBox(width: 200)],
        ),
        error: (e, _) => Row(
          children: [
            const Expanded(child: Text('Could not load announcements.')),
            TextButton(
              onPressed: () => ref.invalidate(latestAnnouncementsProvider),
              child: const Text('Try again'),
            ),
          ],
        ),
        data: (c) => c.value.isEmpty
            ? const Text('No announcements yet.')
            : Column(
                children: [
                  for (final a in c.value)
                    ListTile(
                      key: ValueKey('home-ann-${a.id}'),
                      contentPadding: EdgeInsets.zero,
                      dense: true,
                      leading: a.isRead
                          ? const Icon(Icons.article_outlined)
                          : Icon(Icons.circle, size: 12, color: theme.colorScheme.primary),
                      title: Text(
                        a.title,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(fontWeight: a.isRead ? FontWeight.w400 : FontWeight.w700),
                      ),
                      subtitle: Text(
                        [
                          if (a.priority != AnnouncementPriority.normal) a.priority.label,
                          relativeTime(a.publishedAt, DateTime.now()),
                        ].where((e) => e.isNotEmpty).join(' - '),
                      ),
                      onTap: () => onOpen(a),
                    ),
                ],
              ),
      ),
    );
  }
}

/// Today's and upcoming birthdays / anniversaries, with a copyable wish.
class CelebrationsCard extends StatelessWidget {
  const CelebrationsCard({required this.items, required this.now, super.key, this.max = 5});
  final List<Celebration> items;
  final DateTime now;
  final int max;

  @override
  Widget build(BuildContext context) {
    final shown = items.take(max).toList();
    return HomeCard(
      title: 'Birthdays and anniversaries',
      icon: Icons.cake_outlined,
      child: shown.isEmpty
          ? const Text('Nothing to celebrate in the next few days.')
          : Column(
              children: [
                for (final c in shown)
                  ListTile(
                    key: ValueKey('celebration-${c.user.id}-${c.type.name}'),
                    contentPadding: EdgeInsets.zero,
                    leading: UserAvatar(name: c.user.display, photoUrl: c.user.photoUrl),
                    title: Text(c.user.display),
                    subtitle: Text('${c.typeLabel} - ${c.whenLabel(now)}'),
                    trailing: IconButton(
                      tooltip: 'Copy a wish for ${c.user.display}',
                      icon: const Icon(Icons.celebration_outlined),
                      onPressed: () async {
                        final messenger = ScaffoldMessenger.of(context);
                        await Clipboard.setData(ClipboardData(text: c.wish()));
                        messenger.showSnackBar(
                          const SnackBar(content: Text('Wish copied. Paste it in your chat app.')),
                        );
                      },
                    ),
                  ),
                if (items.length > max)
                  Align(
                    alignment: Alignment.centerLeft,
                    child: Text('and ${items.length - max} more'),
                  ),
              ],
            ),
    );
  }
}
