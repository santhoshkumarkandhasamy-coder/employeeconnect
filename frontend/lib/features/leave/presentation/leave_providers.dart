import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/cache/json_cache.dart';
import '../data/leave_repository.dart';
import '../domain/leave_models.dart';

final leaveTypesProvider = FutureProvider.autoDispose<Cached<List<LeaveType>>>(
  (ref) => ref.watch(leaveRepositoryProvider).types(),
);

final leaveBalanceProvider = FutureProvider.autoDispose<Cached<BalanceData>>(
  (ref) => ref.watch(leaveRepositoryProvider).balance(DateTime.now().year),
);

final onDutyLocationsProvider = FutureProvider.autoDispose<Cached<List<ClientLocationOption>>>(
  (ref) => ref.watch(leaveRepositoryProvider).onDutyLocations(),
);

final myRequestsProvider = FutureProvider.autoDispose<Cached<List<RequestItem>>>(
  (ref) => ref.watch(leaveRepositoryProvider).myRequests(),
);

/// Status filter chip on My Requests (null = All).
final requestStatusFilterProvider = StateProvider.autoDispose<RequestStatus?>((ref) => null);

/// Refreshes everything a new / cancelled / decided request can change.
void refreshLeaveData(WidgetRef ref) {
  ref
    ..invalidate(leaveBalanceProvider)
    ..invalidate(myRequestsProvider);
}

// ---- approvals ---------------------------------------------------------------

typedef ApprovalsQuery = ({RequestKind kind, RequestStatus status});

class ApprovalsState {
  const ApprovalsState({
    required this.items,
    required this.page,
    required this.hasMore,
    this.loadingMore = false,
    this.loadMoreError,
  });
  final List<RequestItem> items;
  final int page;
  final bool hasMore;
  final bool loadingMore;
  final String? loadMoreError;

  ApprovalsState copyWith({
    List<RequestItem>? items,
    int? page,
    bool? hasMore,
    bool? loadingMore,
    String? loadMoreError,
  }) =>
      ApprovalsState(
        items: items ?? this.items,
        page: page ?? this.page,
        hasMore: hasMore ?? this.hasMore,
        loadingMore: loadingMore ?? this.loadingMore,
        loadMoreError: loadMoreError,
      );
}

class ApprovalsNotifier extends AutoDisposeFamilyAsyncNotifier<ApprovalsState, ApprovalsQuery> {
  @override
  Future<ApprovalsState> build(ApprovalsQuery arg) async {
    final p = await ref
        .watch(leaveRepositoryProvider)
        .adminRequests(kind: arg.kind, status: arg.status);
    return ApprovalsState(items: p.items, page: p.page, hasMore: p.hasMore);
  }

  Future<void> loadMore() async {
    final s = state.valueOrNull;
    if (s == null || s.loadingMore || !s.hasMore) return;
    state = AsyncData(s.copyWith(loadingMore: true));
    try {
      final p = await ref
          .read(leaveRepositoryProvider)
          .adminRequests(kind: arg.kind, status: arg.status, page: s.page + 1);
      state = AsyncData(s.copyWith(
        items: [...s.items, ...p.items],
        page: p.page,
        hasMore: p.hasMore,
      ),);
    } on ApiException catch (e) {
      state = AsyncData(s.copyWith(loadingMore: false, loadMoreError: e.message));
    }
  }

  /// Approves / rejects [item]. Throws [ApiException] on failure; when the
  /// request was already decided the list is reloaded.
  Future<void> decide(RequestItem item, {required bool approve, String? comment}) async {
    final repo = ref.read(leaveRepositoryProvider);
    try {
      await repo.decide(item.kind, item.id, approve: approve, comment: comment);
    } on ApiException catch (e) {
      if (e.code == 'NOT_PENDING' || e.code == 'NOT_FOUND') ref.invalidateSelf();
      rethrow;
    }
    final s = state.valueOrNull;
    if (s != null) {
      state = AsyncData(s.copyWith(items: [
        for (final i in s.items)
          if (i.id != item.id || i.kind != item.kind) i,
      ],),);
    }
  }
}

final approvalsProvider = AsyncNotifierProvider.autoDispose
    .family<ApprovalsNotifier, ApprovalsState, ApprovalsQuery>(ApprovalsNotifier.new);
