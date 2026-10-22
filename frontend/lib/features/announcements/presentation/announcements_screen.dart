import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/widgets/state_views.dart';
import '../../attendance/presentation/widgets/summary_card.dart' show StaleBanner;
import '../domain/announcement_models.dart';
import 'announcement_detail_screen.dart';
import 'announcement_providers.dart';
import 'announcement_widgets.dart';

/// Employee list: priority badges, unread dot, pull to refresh, infinite
/// scroll. Urgent items are pinned at the top (plan 06 O1).
class AnnouncementsScreen extends ConsumerStatefulWidget {
  const AnnouncementsScreen({super.key, this.clock});

  /// Test hook for relative times.
  final DateTime Function()? clock;

  @override
  ConsumerState<AnnouncementsScreen> createState() => _AnnouncementsScreenState();
}

class _AnnouncementsScreenState extends ConsumerState<AnnouncementsScreen> {
  bool _unreadOnly = false;
  final _scroll = ScrollController();

  @override
  void initState() {
    super.initState();
    _scroll.addListener(() {
      if (_scroll.hasClients && _scroll.position.extentAfter < 300) {
        ref.read(announcementListProvider(_unreadOnly).notifier).loadMore();
      }
    });
  }

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _refresh() async {
    ref.invalidate(unreadCountProvider);
    await ref.read(announcementListProvider(_unreadOnly).notifier).reload();
  }

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(announcementListProvider(_unreadOnly));
    final now = (widget.clock ?? DateTime.now)();

    return SecureScreen(
        child: Scaffold(
      appBar: AppBar(title: const Text('Announcements')),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
            child: SegmentedButton<bool>(
              segments: const [
                ButtonSegment(value: false, label: Text('All')),
                ButtonSegment(value: true, label: Text('Unread')),
              ],
              selected: {_unreadOnly},
              onSelectionChanged: (s) => setState(() => _unreadOnly = s.first),
            ),
          ),
          Expanded(
            child: async.when(
              skipLoadingOnReload: true,
              loading: () => const LoadingView(label: 'Loading announcements'),
              error: (e, _) => ErrorView(
                message: e is ApiException ? announcementErrorMessage(e) : 'Could not load announcements.',
                onRetry: _refresh,
              ),
              data: (s) => RefreshIndicator(
                onRefresh: _refresh,
                child: s.items.isEmpty
                    ? ListView(
                        physics: const AlwaysScrollableScrollPhysics(),
                        children: [
                          if (s.stale) const Padding(padding: EdgeInsets.all(12), child: StaleBanner()),
                          const SizedBox(height: 80),
                          EmptyView(
                            icon: Icons.campaign_outlined,
                            message: _unreadOnly
                                ? 'You are all caught up. No unread announcements.'
                                : 'No announcements yet.',
                          ),
                        ],
                      )
                    : ListView.builder(
                        controller: _scroll,
                        physics: const AlwaysScrollableScrollPhysics(),
                        padding: const EdgeInsets.fromLTRB(12, 4, 12, 24),
                        itemCount: s.items.length + 1 + (s.stale ? 1 : 0),
                        itemBuilder: (context, i) {
                          if (s.stale) {
                            if (i == 0) return const StaleBanner();
                            i--;
                          }
                          if (i == s.items.length) return _Footer(state: s, unreadOnly: _unreadOnly);
                          final a = s.items[i];
                          return AnnouncementTile(
                            item: a,
                            now: now,
                            onTap: () => Navigator.of(context).push(
                              MaterialPageRoute<void>(
                                builder: (_) => AnnouncementDetailScreen(id: a.id, initial: a),
                              ),
                            ),
                          );
                        },
                      ),
              ),
            ),
          ),
        ],
      ),
    ),);
  }
}

class _Footer extends ConsumerWidget {
  const _Footer({required this.state, required this.unreadOnly});
  final PagedList<Announcement> state;
  final bool unreadOnly;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (state.loadingMore) {
      return const Padding(padding: EdgeInsets.all(16), child: Center(child: CircularProgressIndicator()));
    }
    if (state.loadMoreError != null) {
      return Padding(
        padding: const EdgeInsets.all(8),
        child: Column(
          children: [
            Text(state.loadMoreError!, textAlign: TextAlign.center),
            TextButton(
              onPressed: () => ref.read(announcementListProvider(unreadOnly).notifier).loadMore(),
              child: const Text('Try again'),
            ),
          ],
        ),
      );
    }
    return const SizedBox(height: 8);
  }
}
