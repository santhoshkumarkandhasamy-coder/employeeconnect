import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/cache/json_cache.dart';
import '../../attendance/data/attendance_repository.dart' show cacheScopeProvider;
import '../data/announcement_repository.dart';
import '../domain/announcement_models.dart';

/// A growing, de-duplicated page list.
class PagedList<T> {
  const PagedList({
    required this.items,
    required this.page,
    required this.hasMore,
    this.stale = false,
    this.loadingMore = false,
    this.loadMoreError,
  });
  final List<T> items;
  final int page;
  final bool hasMore;

  /// Showing the offline copy.
  final bool stale;
  final bool loadingMore;
  final String? loadMoreError;

  PagedList<T> copyWith({
    List<T>? items,
    int? page,
    bool? hasMore,
    bool? loadingMore,
    String? loadMoreError,
  }) =>
      PagedList<T>(
        items: items ?? this.items,
        page: page ?? this.page,
        hasMore: hasMore ?? this.hasMore,
        stale: stale,
        loadingMore: loadingMore ?? this.loadingMore,
        loadMoreError: loadMoreError,
      );
}

String _msg(Object e) => e is ApiException ? announcementErrorMessage(e) : 'Something went wrong.';

/// Employee list. Family argument: unread only.
class AnnouncementListNotifier
    extends AutoDisposeFamilyAsyncNotifier<PagedList<Announcement>, bool> {
  AnnouncementRepository get _repo => ref.read(announcementRepositoryProvider);

  @override
  Future<PagedList<Announcement>> build(bool arg) async {
    final r = await ref.watch(announcementRepositoryProvider).list(unreadOnly: arg);
    return PagedList(
      items: pinUrgent(r.value.items),
      page: r.value.page,
      hasMore: r.value.hasMore && !r.stale,
      stale: r.stale,
    );
  }

  Future<void> loadMore() async {
    final s = state.valueOrNull;
    if (s == null || s.loadingMore || !s.hasMore || s.stale) return;
    state = AsyncData(s.copyWith(loadingMore: true));
    try {
      final r = await _repo.list(page: s.page + 1, unreadOnly: arg);
      final cur = state.valueOrNull ?? s;
      state = AsyncData(cur.copyWith(
        items: pinUrgent(mergePages(cur.items, r.value.items)),
        page: r.value.page,
        hasMore: r.value.hasMore,
        loadingMore: false,
      ),);
    } on Object catch (e) {
      final cur = state.valueOrNull ?? s;
      state = AsyncData(cur.copyWith(loadingMore: false, loadMoreError: _msg(e)));
    }
  }

  /// Local update after the user opened an item (no refetch, keeps scroll).
  void markLocalRead(String id) {
    final s = state.valueOrNull;
    if (s == null) return;
    state = AsyncData(s.copyWith(items: [
      for (final a in s.items) a.id == id ? a.copyWith(isRead: true) : a,
    ],),);
  }

  Future<void> reload() async {
    ref.invalidateSelf();
    await future.then((_) {}, onError: (Object _) {});
  }
}

final announcementListProvider = AsyncNotifierProvider.autoDispose
    .family<AnnouncementListNotifier, PagedList<Announcement>, bool>(
  AnnouncementListNotifier.new,
);

/// Number shown on the More tab and Home. Failures read as 0 (no badge):
/// a badge must never break navigation.
final unreadCountProvider = FutureProvider<int>((ref) async {
  ref.watch(cacheScopeProvider); // new user -> new count
  try {
    return await ref.watch(announcementRepositoryProvider).unreadCount();
  } on ApiException {
    return 0;
  }
});

/// Marks [id] read on the server and updates list + badge. Failures are
/// swallowed (the item simply stays unread and is retried next time). Takes a
/// [ProviderContainer] (`ProviderScope.containerOf(context)`) so it still
/// works when the calling screen was closed meanwhile.
Future<void> markAnnouncementRead(ProviderContainer c, String id) async {
  try {
    await c.read(announcementRepositoryProvider).markRead(id);
  } on ApiException {
    return;
  }
  if (c.exists(announcementListProvider(false))) {
    c.read(announcementListProvider(false).notifier).markLocalRead(id);
  }
  c
    ..invalidate(announcementListProvider(true))
    ..invalidate(unreadCountProvider)
    ..invalidate(latestAnnouncementsProvider);
}

/// First few announcements for the Home dashboard.
final latestAnnouncementsProvider =
    FutureProvider.autoDispose<Cached<List<Announcement>>>((ref) async {
  final r = await ref.watch(announcementRepositoryProvider).list(pageSize: 3);
  return Cached(pinUrgent(r.value.items).take(3).toList(), stale: r.stale);
});

final announcementDetailProvider = FutureProvider.autoDispose.family<Announcement, String>(
  (ref, id) => ref.watch(announcementRepositoryProvider).detail(id),
);

// ---- admin ---------------------------------------------------------------------

class AdminAnnouncementsNotifier extends AutoDisposeAsyncNotifier<PagedList<Announcement>> {
  @override
  Future<PagedList<Announcement>> build() async {
    final p = await ref.watch(announcementRepositoryProvider).adminList();
    return PagedList(items: p.items, page: p.page, hasMore: p.hasMore);
  }

  Future<void> loadMore() async {
    final s = state.valueOrNull;
    if (s == null || s.loadingMore || !s.hasMore) return;
    state = AsyncData(s.copyWith(loadingMore: true));
    try {
      final p = await ref.read(announcementRepositoryProvider).adminList(page: s.page + 1);
      final cur = state.valueOrNull ?? s;
      state = AsyncData(cur.copyWith(
        items: mergePages(cur.items, p.items),
        page: p.page,
        hasMore: p.hasMore,
        loadingMore: false,
      ),);
    } on Object catch (e) {
      final cur = state.valueOrNull ?? s;
      state = AsyncData(cur.copyWith(loadingMore: false, loadMoreError: _msg(e)));
    }
  }

  /// Soft delete on the server, then drop the row locally.
  Future<void> delete(String id) async {
    await ref.read(announcementRepositoryProvider).delete(id);
    final s = state.valueOrNull;
    if (s != null) {
      state = AsyncData(s.copyWith(items: [
        for (final a in s.items)
          if (a.id != id) a,
      ],),);
    }
    ref
      ..invalidate(announcementListProvider(false))
      ..invalidate(announcementListProvider(true))
      ..invalidate(unreadCountProvider)
      ..invalidate(latestAnnouncementsProvider);
  }

  Future<void> reload() async {
    ref.invalidateSelf();
    await future.then((_) {}, onError: (Object _) {});
  }
}

final adminAnnouncementsProvider =
    AsyncNotifierProvider.autoDispose<AdminAnnouncementsNotifier, PagedList<Announcement>>(
  AdminAnnouncementsNotifier.new,
);

/// Call after create / update so employee lists and the admin list refresh.
void refreshAnnouncements(WidgetRef ref) {
  ref
    ..invalidate(adminAnnouncementsProvider)
    ..invalidate(announcementListProvider(false))
    ..invalidate(announcementListProvider(true))
    ..invalidate(unreadCountProvider)
    ..invalidate(latestAnnouncementsProvider);
}

final departmentsProvider = FutureProvider.autoDispose<List<Department>>(
  (ref) => ref.watch(announcementRepositoryProvider).departments(),
);

final readStatsProvider = FutureProvider.autoDispose.family<ReadStats, String>(
  (ref, id) => ref.watch(announcementRepositoryProvider).reads(id),
);
