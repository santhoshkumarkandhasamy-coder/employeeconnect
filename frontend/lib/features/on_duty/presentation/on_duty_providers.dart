import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../attendance/data/location_service.dart';
import '../../attendance/data/selfie_service.dart';
import '../../attendance/domain/attendance_models.dart';
import '../../attendance/presentation/attendance_providers.dart';
import '../../leave/presentation/leave_providers.dart' show refreshLeaveData;
import '../data/on_duty_repository.dart';
import '../domain/on_duty_config.dart';
import '../domain/on_duty_models.dart';
import 'on_duty_data_providers.dart';

export 'on_duty_data_providers.dart';

/// `/config.on_duty`. Hidden (none) until the config has loaded or when the
/// server predates On Duty.
final onDutyConfigProvider = Provider<OnDutyConfig>(
  (ref) => ref.watch(attendanceConfigProvider).valueOrNull?.onDuty ?? OnDutyConfig.none,
);

enum AttendanceMode { office, onDuty }

/// Office | On Duty switch on the Today tab.
final attendanceModeProvider = StateProvider.autoDispose<AttendanceMode>((ref) => AttendanceMode.office);

/// Site chosen for the next check-in.
final selectedSiteProvider = StateProvider.autoDispose<String?>((ref) => null);

/// Explains why location is needed before the first OS prompt (same wording
/// idea as the office flow: one-shot, never in the background).
const onDutyRationale =
    'Employee Connect reads your location once when you check in at a site and once when '
    'you check out, to confirm you are at that site. It is not tracked in the background.';

/// Drives visit check-in / check-out. One-shot location only: [LocationService.current]
/// is called a fixed number of times per punch and nothing is streamed.
class VisitController extends AutoDisposeNotifier<CheckState> {
  @override
  CheckState build() {
    final selfies = ref.read(selfieServiceProvider);
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

  CheckState _fail(String message, {LocationAccess? blocked, bool? selfieNeeded}) => CheckState(
        error: message,
        blockedAccess: blocked,
        selfiePath: state.selfiePath,
        selfieNeeded: selfieNeeded ?? state.selfieNeeded,
        mockLocation: state.mockLocation,
      );

  /// Permission + one fix. Returns null after setting the failure state.
  Future<LocationFix?> _locate(Future<bool> Function() confirmRationale) async {
    final loc = ref.read(locationServiceProvider);
    var access = await loc.access();
    if (access == LocationAccess.denied) {
      if (!await confirmRationale()) {
        state = _fail('Location permission is needed to record your visit.');
        return null;
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
          _ => 'Location permission is needed to record your visit.',
        },
        blocked: access,
      );
      return null;
    }
    final LocationFix fix;
    try {
      fix = await loc.current();
    } on LocationException catch (e) {
      state = _fail(e.message);
      return null;
    }
    final rejection = fixRejection(fix);
    if (rejection != null) {
      state = _fail(rejection);
      return null;
    }
    return fix;
  }

  /// Returns true on success.
  Future<bool> checkIn({
    required String locationId,
    required String locationName,
    required bool selfieRequired,
    required Future<bool> Function() confirmRationale,
    String? note,
  }) async {
    if (state.busy) return false;
    final repo = ref.read(onDutyRepositoryProvider);
    state = CheckState(
      step: CheckStep.locating,
      selfiePath: state.selfiePath,
      selfieNeeded: state.selfieNeeded,
    );
    var fix = await _locate(confirmRationale);
    if (fix == null) return false;
    state = CheckState(
      step: CheckStep.locating,
      selfiePath: state.selfiePath,
      selfieNeeded: state.selfieNeeded,
      mockLocation: fix.isMock,
    );

    String? selfieId;
    final required = selfieRequired || state.selfieNeeded;
    var path = state.selfiePath;
    if (path == null && required) {
      state = CheckState(step: CheckStep.selfie, selfieNeeded: state.selfieNeeded, mockLocation: fix.isMock);
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
          state = _fail('Could not upload your selfie. ${onDutyErrorMessage(e)}');
          return false;
        }
      }
      await ref.read(selfieServiceProvider).discard(path);
      state = CheckState(
        step: CheckStep.uploading,
        selfieNeeded: state.selfieNeeded,
        mockLocation: fix.isMock,
      );
    }

    if (selfieId != null) {
      // Capturing and uploading takes time; the server rejects older fixes.
      try {
        fix = await ref.read(locationServiceProvider).current();
      } on LocationException catch (e) {
        state = _fail(e.message);
        return false;
      }
      final again = fixRejection(fix);
      if (again != null) {
        state = _fail(again);
        return false;
      }
    }

    state = CheckState(
      step: CheckStep.submitting,
      selfieNeeded: state.selfieNeeded,
      mockLocation: fix.isMock,
    );
    try {
      final r = await repo.checkIn(
        locationId: locationId,
        fix: fix,
        selfieFileId: selfieId,
        note: note,
      );
      final at = r.visit.checkInAt ?? DateTime.now();
      state = CheckState(
        success: 'Checked in at $locationName at ${formatTime(at)}',
        mockLocation: fix.isMock,
      );
      _refreshData();
      return true;
    } on ApiException catch (e) {
      _afterError(e);
      state = _fail(
        onDutyErrorMessage(e, locationName: locationName),
        selfieNeeded: isSelfieRetakeError(e) ? true : null,
      );
      return false;
    }
  }

  Future<bool> checkOut({
    required String visitId,
    required String locationName,
    required Future<bool> Function() confirmRationale,
    String? note,
  }) async {
    if (state.busy) return false;
    final repo = ref.read(onDutyRepositoryProvider);
    state = const CheckState(step: CheckStep.locating);
    final fix = await _locate(confirmRationale);
    if (fix == null) return false;
    state = CheckState(step: CheckStep.submitting, mockLocation: fix.isMock);
    try {
      final r = await repo.checkOut(fix: fix, visitId: visitId, note: note);
      final at = r.visit.checkOutAt ?? DateTime.now();
      state = CheckState(
        success: 'Checked out of $locationName at ${formatTime(at)}',
        mockLocation: fix.isMock,
      );
      _refreshData();
      return true;
    } on ApiException catch (e) {
      _afterError(e);
      state = _fail(onDutyErrorMessage(e, locationName: locationName));
      return false;
    }
  }

  void _afterError(ApiException e) {
    if (onDutyNeedsReload(e)) {
      ref
        ..invalidate(onDutyTodayProvider)
        ..invalidate(todayAttendanceProvider);
    }
    if (e.code == 'DUTY_LOCATION_UNAVAILABLE' || e.code == 'ON_DUTY_DISABLED') {
      ref
        ..invalidate(dutyLocationsProvider)
        ..invalidate(attendanceConfigProvider);
    }
  }

  void _refreshData() {
    ref
      ..invalidate(onDutyTodayProvider)
      ..invalidate(todayAttendanceProvider)
      ..invalidate(historyProvider)
      ..invalidate(calendarProvider)
      ..invalidate(attendanceSummaryProvider);
    // The position list is not re-read here: the next punch always takes a fresh
    // fix, and the refresh button re-reads it on demand.
  }
}

final visitControllerProvider =
    NotifierProvider.autoDispose<VisitController, CheckState>(VisitController.new);

/// Reloads everything that an On Duty request can change.
void refreshOnDutyRequests(WidgetRef ref) {
  refreshLeaveData(ref);
  ref.invalidate(onDutyTodayProvider);
}
