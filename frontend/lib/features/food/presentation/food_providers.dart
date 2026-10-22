import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../data/food_repository.dart';
import '../domain/food_models.dart';

/// Device clock; overridable in tests.
final foodClockProvider = Provider<DateTime Function()>((_) => DateTime.now);

class FoodState {
  const FoodState({
    required this.days,
    this.stale = false,
    this.offset = Duration.zero,
    this.pending = const {},
  });
  final List<FoodDay> days;
  final bool stale;

  /// Server clock minus device clock.
  final Duration offset;

  /// Dates with a save in flight (their switch is disabled).
  final Set<String> pending;

  FoodState copyWith({List<FoodDay>? days, Duration? offset, Set<String>? pending}) => FoodState(
        days: days ?? this.days,
        stale: stale,
        offset: offset ?? this.offset,
        pending: pending ?? this.pending,
      );
}

class FoodNotifier extends AutoDisposeAsyncNotifier<FoodState> {
  FoodRepository get _repo => ref.read(foodRepositoryProvider);
  DateTime _deviceNow() => ref.read(foodClockProvider)();

  @override
  Future<FoodState> build() async {
    final r = await ref.watch(foodRepositoryProvider).myEnrollments();
    final offset = r.stale ? Duration.zero : _repo.serverOffset;
    return FoodState(
      days: upcomingOnly(r.value, serverNow(_deviceNow(), offset)),
      stale: r.stale,
      offset: offset,
    );
  }

  List<FoodDay> _replace(List<FoodDay> days, Iterable<FoodDay> updated) {
    final m = {for (final d in updated) d.date: d};
    return [for (final d in days) m[d.date] ?? d];
  }

  /// Optimistic Yes/No for one date. Returns null on success, else a message
  /// (the switch has been rolled back by then).
  Future<String?> toggle(String date, {required bool needFood}) async {
    final s = state.valueOrNull;
    if (s == null || s.pending.contains(date)) return null;
    final before = s.days.firstWhere((d) => d.date == date);
    state = AsyncData(s.copyWith(
      days: _replace(s.days, [before.copyWith(needFood: needFood)]),
      pending: {...s.pending, date},
    ),);
    try {
      final saved = await _repo.setOne(date, needFood: needFood);
      final cur = state.valueOrNull ?? s;
      final merged = FoodDay(
        date: date,
        needFood: saved.needFood ?? needFood,
        canChange: saved.canChange,
        cutoffAt: saved.cutoffAt ?? before.cutoffAt,
        isHoliday: before.isHoliday,
        isWeeklyOff: before.isWeeklyOff,
      );
      final days = _replace(cur.days, [merged]);
      state = AsyncData(cur.copyWith(
        days: days,
        offset: _repo.serverOffset,
        pending: {...cur.pending}..remove(date),
      ),);
      await _repo.remember(days);
      return null;
    } on ApiException catch (e) {
      final cur = state.valueOrNull ?? s;
      state = AsyncData(cur.copyWith(
        days: _replace(cur.days, [before]),
        pending: {...cur.pending}..remove(date),
      ),);
      if (e.code == 'FOOD_CUTOFF_PASSED' || e.code == 'INVALID_DATE') {
        // Our view was out of date; reload quietly to pick up the server state.
        ref.invalidateSelf();
      }
      return foodErrorMessage(e);
    }
  }

  /// Optimistic bulk set for [dates]. Failed dates roll back individually.
  /// Returns a user message (null when everything saved).
  Future<String?> setMany(List<String> dates, {required bool needFood}) async {
    final s = state.valueOrNull;
    if (s == null || dates.isEmpty) return null;
    final before = {for (final d in s.days) d.date: d};
    final targets = [for (final d in dates) if (before[d] != null) before[d]!];
    state = AsyncData(s.copyWith(
      days: _replace(s.days, [for (final d in targets) d.copyWith(needFood: needFood)]),
      pending: {...s.pending, ...dates},
    ),);
    void rollback(Iterable<String> ds, {bool all = false}) {
      final cur = state.valueOrNull ?? s;
      state = AsyncData(cur.copyWith(
        days: _replace(cur.days, [for (final d in ds) if (before[d] != null) before[d]!]),
        pending: {...cur.pending}..removeAll(all ? dates : ds),
      ),);
    }

    try {
      final r = await _repo.setMany(dates, needFood: needFood);
      final failedDates = {for (final f in r.failed) f.date};
      final cur = state.valueOrNull ?? s;
      final okDays = [
        for (final d in r.items)
          if (before[d.date] != null && !failedDates.contains(d.date))
            FoodDay(
              date: d.date,
              needFood: d.needFood ?? needFood,
              canChange: d.canChange,
              cutoffAt: d.cutoffAt ?? before[d.date]!.cutoffAt,
              isHoliday: before[d.date]!.isHoliday,
              isWeeklyOff: before[d.date]!.isWeeklyOff,
            ),
      ];
      state = AsyncData(cur.copyWith(
        days: _replace(cur.days, okDays),
        offset: _repo.serverOffset,
        pending: {...cur.pending}..removeAll(dates),
      ),);
      // Dates the server neither confirmed nor reported failed: undo.
      final confirmed = {for (final d in okDays) d.date};
      rollback([
        for (final d in dates)
          if (!confirmed.contains(d)) d,
      ]);
      await _repo.remember(state.valueOrNull!.days);
      if (r.failed.isEmpty) return null;
      final reasons = {for (final f in r.failed) f.code};
      final why = reasons.length == 1
          ? foodErrorMessage(ApiException(code: reasons.first, message: 'Some days could not be saved.'))
          : 'Some days could not be saved.';
      return '${r.failed.length} day(s) not saved. $why';
    } on ApiException catch (e) {
      rollback(dates, all: true);
      return foodErrorMessage(e);
    }
  }

  Future<void> reload() async {
    ref.invalidateSelf();
    await future.then((_) {}, onError: (Object _) {});
  }
}

final foodProvider =
    AsyncNotifierProvider.autoDispose<FoodNotifier, FoodState>(FoodNotifier.new);

// ---- admin ----------------------------------------------------------------

/// Date shown by the report (`YYYY-MM-DD`); defaults to tomorrow's planning
/// date is not assumed, so start on today.
final foodReportDateProvider =
    StateProvider.autoDispose<String>((ref) => ymd(ref.read(foodClockProvider)()));

final foodReportProvider = FutureProvider.autoDispose.family<FoodDailyReport, String>(
  (ref, date) => ref.watch(foodRepositoryProvider).daily(date),
);
