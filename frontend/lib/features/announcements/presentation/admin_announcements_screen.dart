import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../auth/presentation/auth_controller.dart';
import '../../auth/presentation/auth_state.dart';
import '../domain/announcement_models.dart';
import 'announcement_form_screen.dart';
import 'announcement_providers.dart';
import 'announcement_reads_screen.dart';
import 'announcement_widgets.dart';

/// Admin / HR list (includes expired items). Needs `announcement.manage`;
/// the server enforces it as well.
class AdminAnnouncementsScreen extends ConsumerStatefulWidget {
  const AdminAnnouncementsScreen({super.key});

  @override
  ConsumerState<AdminAnnouncementsScreen> createState() => _AdminAnnouncementsScreenState();
}

class _AdminAnnouncementsScreenState extends ConsumerState<AdminAnnouncementsScreen> {
  final _scroll = ScrollController();

  @override
  void initState() {
    super.initState();
    _scroll.addListener(() {
      if (_scroll.hasClients && _scroll.position.extentAfter < 300) {
        ref.read(adminAnnouncementsProvider.notifier).loadMore();
      }
    });
  }

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _open({Announcement? existing}) async {
    await Navigator.of(context).push(
      MaterialPageRoute<void>(builder: (_) => AnnouncementFormScreen(existing: existing)),
    );
  }

  Future<void> _delete(Announcement a) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Delete announcement?'),
        content: Text('"${a.title}" will disappear for everyone. This cannot be undone.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    final messenger = ScaffoldMessenger.of(context);
    try {
      await ref.read(adminAnnouncementsProvider.notifier).delete(a.id);
      messenger.showSnackBar(const SnackBar(content: Text('Announcement deleted.')));
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(announcementErrorMessage(e))));
    }
  }

  @override
  Widget build(BuildContext context) {
    final allowed = ref.watch(authControllerProvider.select(
      (s) => s is AuthAuthenticated && canManageAnnouncements(s.user.permissions),
    ),);
    if (!allowed) {
      return Scaffold(
        appBar: AppBar(title: const Text('Manage announcements')),
        body: const EmptyView(
          icon: Icons.lock_outline,
          message: 'You do not have permission to manage announcements.',
        ),
      );
    }
    final async = ref.watch(adminAnnouncementsProvider);
    final now = DateTime.now();

    return Scaffold(
      appBar: AppBar(title: const Text('Manage announcements')),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: _open,
        icon: const Icon(Icons.add),
        label: const Text('New'),
      ),
      body: async.when(
        skipLoadingOnReload: true,
        loading: () => const LoadingView(label: 'Loading announcements'),
        error: (e, _) => ErrorView(
          message: e is ApiException ? announcementErrorMessage(e) : 'Could not load announcements.',
          onRetry: () => ref.read(adminAnnouncementsProvider.notifier).reload(),
        ),
        data: (s) => RefreshIndicator(
          onRefresh: () => ref.read(adminAnnouncementsProvider.notifier).reload(),
          child: s.items.isEmpty
              ? ListView(
                  physics: const AlwaysScrollableScrollPhysics(),
                  children: const [
                    SizedBox(height: 120),
                    EmptyView(icon: Icons.campaign_outlined, message: 'No announcements yet. Tap New to create one.'),
                  ],
                )
              : ListView.builder(
                  controller: _scroll,
                  physics: const AlwaysScrollableScrollPhysics(),
                  padding: const EdgeInsets.fromLTRB(12, 8, 12, 96),
                  itemCount: s.items.length + 1,
                  itemBuilder: (context, i) {
                    if (i == s.items.length) {
                      return s.loadingMore
                          ? const Padding(
                              padding: EdgeInsets.all(16),
                              child: Center(child: CircularProgressIndicator()),
                            )
                          : s.loadMoreError != null
                              ? TextButton(
                                  onPressed: () => ref.read(adminAnnouncementsProvider.notifier).loadMore(),
                                  child: Text('${s.loadMoreError} Try again'),
                                )
                              : const SizedBox.shrink();
                    }
                    final a = s.items[i];
                    final expired = a.expiresAt != null && !a.expiresAt!.isAfter(now);
                    final reads = a.readCount != null && a.targetCount != null
                        ? '${a.readCount}/${a.targetCount} read'
                        : null;
                    return Card(
                      margin: const EdgeInsets.symmetric(vertical: 4),
                      child: ListTile(
                        title: Text(a.title, maxLines: 2, overflow: TextOverflow.ellipsis),
                        subtitle: Padding(
                          padding: const EdgeInsets.only(top: 4),
                          child: Wrap(
                            spacing: 8,
                            runSpacing: 4,
                            crossAxisAlignment: WrapCrossAlignment.center,
                            children: [
                              PriorityBadge(a.priority),
                              Text(a.targetAll ? 'Everyone' : '${a.departmentIds.length} dept(s)'),
                              if (reads != null) Text(reads),
                              if (expired) const Text('Expired'),
                              if (a.publishedAt != null) Text(formatDayLong(a.publishedAt!)),
                            ],
                          ),
                        ),
                        onTap: () => _open(existing: a),
                        trailing: PopupMenuButton<String>(
                          tooltip: 'Actions for ${a.title}',
                          onSelected: (v) {
                            switch (v) {
                              case 'edit':
                                _open(existing: a);
                              case 'reads':
                                Navigator.of(context).push(
                                  MaterialPageRoute<void>(
                                    builder: (_) => AnnouncementReadsScreen(announcement: a),
                                  ),
                                );
                              case 'delete':
                                _delete(a);
                            }
                          },
                          itemBuilder: (_) => const [
                            PopupMenuItem(value: 'edit', child: Text('Edit')),
                            PopupMenuItem(value: 'reads', child: Text('Read statistics')),
                            PopupMenuItem(value: 'delete', child: Text('Delete')),
                          ],
                        ),
                      ),
                    );
                  },
                ),
        ),
      ),
    );
  }
}
