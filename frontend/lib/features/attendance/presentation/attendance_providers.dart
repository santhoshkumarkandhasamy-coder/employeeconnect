import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/cache/json_cache.dart';
import '../../../core/utils/dates.dart';
import '../../../core/utils/geo.dart';
import '../data/attendance_repository.dart';
import '../data/location_service.dart';
import '../data/selfie_service.dart';
import '../domain/attendance_models.dart';

/// `/config` attendance bits. Falls back to defaults if it cannot be fetched
/// (the server still enforces `SELFIE_REQUIRED`).
final attendanceConfigProvider = FutureProvider<AttendanceConfig>((ref) async {
  try {
    return await ref.watch(attendanceRepositoryProvider).config();
  } on ApiException {
    return AttendanceConfig.fallback;
  }
});

/// Today's record (null when not checked in) and today's on-duty site, if any.
/// Also feeds the home card.
final todayAttendanceProvider = FutureProvider.autoDispose<TodayAttendance>(
  (ref) => ref.watch(attendanceRepositoryProvider).today(),
);

/// Month shown by the calendar tab (`YYYY-MM`).
final attendanceMonthProvider =
    StateProvider.autoDispose<String>((ref) => monthKey(DateTime.now()));

final calendarProvider = FutureProvider.autoDispose.family<Cached<CalendarMonth>, String>(
  (ref, month) => ref.watch(attendanceRepositoryProvider).calendar(month),
);

final attendanceSummaryProvider =
    FutureProvider.autoDispose.family<Cached<AttendanceSummary>, String>(
  (ref, month) => ref.watch(attendanceRepositoryProvider).summary(month),
);

// ---- history (paginated) ---------------------------------------------------

class HistoryState {
  const HistoryState({
    required this.items,
    required this.page,
    required this.hasMore,
    this.stale = false,
    this.loadingMore = false,
    this.loadMoreError,
  });
  final List<AttendanceRecord> items;
  final int page;
  final bool hasMore;
  final bool stale;
  final bool loadingMore;
  final String? loadMoreError;

  HistoryState copyWith({
    List<AttendanceRecord>? items,
    int? page,
    bool? hasMore,
    bool? loadingMore,
    String? loadMoreError,
  }) =>
      HistoryState(
        items: items ?? this.items,
        page: page ?? this.page,
        hasMore: hasMore ?? this.hasMore,
        stale: stale,
        loadingMore: loadingMore ?? this.loadingMore,
        loadMoreError: loadMoreError,
      );
}

class HistoryNotifier extends AutoDisposeAsyncNotifier<HistoryState> {
  @override
  Future<HistoryState> build() async {
    final r = await ref.watch(attendanceRepositoryProvider).history();
    return HistoryState(
      items: r.value.items,
      page: r.value.page,
      hasMore: r.value.hasMore && !r.stale,
      stale: r.stale,
    );
  }

  Future<void> loadMore() async {
    final s = state.valueOrNull;
    if (s == null || s.loadingMore || !s.hasMore) return;
    state = AsyncData(s.copyWith(loadingMore: true));
    try {
      final r = await ref.read(attendanceRepositoryProvider).history(page: s.page + 1);
      state = AsyncData(s.copyWith(
        items: [...s.items, ...r.value.items],
        page: r.value.page,
        hasMore: r.value.hasMore,
      ),);
    } on ApiException catch (e) {
      state = AsyncData(s.copyWith(loadingMore: false, loadMoreError: e.message));
    }
  }
}

final historyProvider =
    AsyncNotifierProvider.autoDispose<HistoryNotifier, HistoryState>(HistoryNotifier.new);

// ---- live proximity to the office ------------------------------------------

class Proximity {
  const Proximity({required this.access, this.fix, this.distanceM, this.radiusM, this.site});
  final LocationAccess access;
  final LocationFix? fix;
  final int? distanceM;
  final int? radiusM;

  /// Today's on-duty client site; the distance is measured to it instead of the office.
  final OnDutySite? site;

  bool? get inside => distanceM == null || radiusM == null ? null : distanceM! <= radiusM!;
}

/// Reads the current position WITHOUT prompting. Invalidate to refresh.
final proximityProvider = FutureProvider.autoDispose<Proximity>((ref) async {
  final loc = ref.watch(locationServiceProvider);
  final access = await loc.access();
  if (access != LocationAccess.granted) return Proximity(access: access);
  final cfg = await ref.watch(attendanceConfigProvider.future);
  OnDutySite? site;
  try {
    site = (await ref.watch(todayAttendanceProvider.future)).onDuty;
  } on ApiException {
    site = null; // the Today tab shows that error; fall back to the office here
  }
  try {
    final fix = await loc.current();
    if (site != null) {
      final d = site.hasCoordinates ? haversineMeters(fix.lat, fix.lng, site.lat!, site.lng!).round() : null;
      return Proximity(access: access, fix: fix, distanceM: d, radiusM: site.radiusM, site: site);
    }
    final d = cfg.hasOffice
        ? haversineMeters(fix.lat, fix.lng, cfg.officeLat!, cfg.officeLng!).round()
        : null;
    return Proximity(access: access, fix: fix, distanceM: d, radiusM: cfg.radiusM);
  } on LocationException {
    return Proximity(access: access, site: site);
  }
});

// ---- check-in / check-out --------------------------------------------------

const preciseLocationMessage =
    'Precise location is required to check in. Allow "Precise location" for '
    'Employee Connect in Settings and try again.';

enum CheckStep { idle, locating, selfie, uploading, submitting }

class CheckState {
  const CheckState({
    this.step = CheckStep.idle,
    this.error,
    this.selfiePath,
    this.blockedAccess,
    this.selfieNeeded = false,
    this.success,
    this.mockLocation = false,
  });
  final CheckStep step;
  final String? error;
  final String? selfiePath;

  /// Set when location is unusable; the UI offers to open settings.
  final LocationAccess? blockedAccess;

  /// The server asked for a selfie although config said optional.
  final bool selfieNeeded;
  final String? success;
  final bool mockLocation;

  bool get busy => step != CheckStep.idle;
}

class CheckController extends AutoDisposeNotifier<CheckState> {
  @override
  CheckState build() {
    final selfies = ref.read(selfieServiceProvider);
    // Never leave a captured selfie behind when the screen goes away.
    ref.onDispose(() {
      final p = state.selfiePath;
      if (p != null) selfies.discard(p);
    });
    return const CheckState();
  }

  Future<void> pickSelfie() async {
    final selfies = ref.read(selfieServiceProvider);
    final old = state.selfiePath;
    final path = await selfies.capture();
    if (path != null) {
      if (old != null && old != path) await selfies.discard(old);
      state = CheckState(selfiePath: path, selfieNeeded: state.selfieNeeded);
    }
  }

  void clearSelfie() {
    final old = state.selfiePath;
    if (old != null) ref.read(selfieServiceProvider).discard(old);
    state = CheckState(selfieNeeded: state.selfieNeeded);
  }

  CheckState _fail(String message, {LocationAccess? blocked, bool? selfieNeeded}) =>
      CheckState(
        error: message,
        blockedAccess: blocked,
        selfiePath: state.selfiePath,
        selfieNeeded: selfieNeeded ?? state.selfieNeeded,
        mockLocation: state.mockLocation,
      );

  /// Runs the full flow. [confirmRationale] shows the in-app explanation
  /// before the first OS permission prompt; return false if the user declines.
  /// Returns true on success.
  Future<bool> submit({
    required bool checkOut,
    required Future<bool> Function() confirmRationale,
  }) async {
    if (state.busy) return false;
    final loc = ref.read(locationServiceProvider);
    final repo = ref.read(attendanceRepositoryProvider);
    state = CheckState(
      step: CheckStep.locating,
      selfiePath: state.selfiePath,
      selfieNeeded: state.selfieNeeded,
    );

    var access = await loc.access();
    if (access == LocationAccess.denied) {
      if (!await confirmRationale()) {
        state = _fail('Location permission is needed to record your attendance.');
        return false;
      }
      access = await loc.request();
    }
    if (access != LocationAccess.granted) {
      state = _fail(
        switch (access) {
          LocationAccess.serviceOff => 'Turn on location services to continue.',
          LocationAccess.reducedAccuracy => preciseLocationMessage,
          LocationAccess.deniedForever =>
            'Location permission is blocked. Enable it in Settings to continue.',
          _ => 'Location permission is needed to record your attendance.',
        },
        blocked: access,
      );
      return false;
    }

    final LocationFix fix;
    try {
      fix = await loc.current();
    } on LocationException catch (e) {
      state = _fail(e.message);
      return false;
    }
    // Refuse to submit a fix with missing or poor accuracy (review 02, H1).
    final rejection = fixRejection(fix);
    if (rejection != null) {
      state = _fail(rejection);
      return false;
    }
    state = CheckState(
      step: CheckStep.locating,
      selfiePath: state.selfiePath,
      selfieNeeded: state.selfieNeeded,
      mockLocation: fix.isMock,
    );

    var punchFix = fix;
    String? selfieId;
    if (!checkOut) {
      final cfg = await ref.read(attendanceConfigProvider.future);
      final required = cfg.selfieRequired || state.selfieNeeded;
      var path = state.selfiePath;
      if (path == null && required) {
        state = CheckState(
          step: CheckStep.selfie,
          selfieNeeded: state.selfieNeeded,
          mockLocation: fix.isMock,
        );
        path = await ref.read(selfieServiceProvider).capture();
        if (path == null) {
          state = _fail('A selfie is required to check in.', selfieNeeded: true);
          return false;
        }
      }
      if (path != null) {
        state = CheckState(
          step: CheckStep.uploading,
          selfiePath: path,
          selfieNeeded: state.selfieNeeded,
          mockLocation: fix.isMock,
        );
        try {
          selfieId = await repo.uploadSelfie(path);
        } on ApiException catch (e) {
          if (required) {
            await ref.read(selfieServiceProvider).discard(path);
            state = CheckState(selfieNeeded: state.selfieNeeded, mockLocation: fix.isMock);
            state = _fail('Could not upload your selfie. ${attendanceErrorMessage(e)}');
            return false;
          }
          // Optional selfie: carry on without it.
        }
        // Uploaded or not, the local copy is deleted (review 02, L11).
        await ref.read(selfieServiceProvider).discard(path);
        state = CheckState(
          step: CheckStep.uploading,
          selfieNeeded: state.selfieNeeded,
          mockLocation: fix.isMock,
        );
      }
    }

    if (selfieId != null) {
      // Capturing/uploading takes time; the server rejects fixes older than ~60 s.
      try {
        punchFix = await loc.current();
      } on LocationException catch (e) {
        state = _fail(e.message);
        return false;
      }
      final again = fixRejection(punchFix);
      if (again != null) {
        state = _fail(again);
        return false;
      }
    }

    state = CheckState(
      step: CheckStep.submitting,
      selfiePath: state.selfiePath,
      selfieNeeded: state.selfieNeeded,
      mockLocation: punchFix.isMock,
    );
    try {
      final rec = checkOut
          ? await repo.checkOut(punchFix)
          : await repo.checkIn(punchFix, selfieFileId: selfieId);
      final at = (checkOut ? rec.checkOutAt : rec.checkInAt) ?? DateTime.now();
      state = CheckState(
        success: checkOut ? 'Checked out at ${formatTime(at)}' : 'Checked in at ${formatTime(at)}',
        mockLocation: punchFix.isMock,
      );
      _refreshData();
      return true;
    } on ApiException catch (e) {
      if (const {'ALREADY_CHECKED_IN', 'ALREADY_CHECKED_OUT', 'NOT_CHECKED_IN', 'ON_LEAVE'}
          .contains(e.code)) {
        ref.invalidate(todayAttendanceProvider);
      }
      if (e.code == 'LOCATION_FIX_STALE' || e.code == 'LOCATION_ACCURACY_LOW') {
        ref.invalidate(proximityProvider);
      }
      // The selfie file was already uploaded (and deleted locally): never
      // reuse its id; the next attempt must capture and upload a fresh one.
      state = _fail(
        attendanceErrorMessage(e),
        selfieNeeded: isSelfieRetakeError(e) ? true : null,
      );
      return false;
    }
  }

  void _refreshData() {
    ref
      ..invalidate(todayAttendanceProvider)
      ..invalidate(historyProvider)
      ..invalidate(calendarProvider)
      ..invalidate(attendanceSummaryProvider)
      ..invalidate(proximityProvider);
  }
}

final checkControllerProvider =
    NotifierProvider.autoDispose<CheckController, CheckState>(CheckController.new);
