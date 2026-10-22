import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/cache/json_cache.dart';
import 'package:employee_connect/features/attendance/data/attendance_repository.dart';
import 'package:employee_connect/features/attendance/data/location_service.dart';
import 'package:employee_connect/features/attendance/data/selfie_service.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart';
import 'package:employee_connect/features/late/data/late_repository.dart';
import 'package:employee_connect/features/late/domain/late_models.dart';

class FakeLocation implements LocationService {
  FakeLocation({
    this.state = LocationAccess.granted,
    this.afterRequest = LocationAccess.granted,
    LocationFix? fix,
  }) : fix = fix ?? LocationFix(lat: 12.97, lng: 77.59, accuracyM: 10, timestamp: DateTime.now());
  LocationAccess state;
  LocationAccess afterRequest;
  LocationFix fix;
  int requests = 0;
  int settingsOpened = 0;

  @override
  Future<LocationAccess> access() async => state;
  @override
  Future<LocationAccess> request() async {
    requests++;
    state = afterRequest;
    return afterRequest;
  }

  @override
  Future<LocationFix> current() async => fix;
  @override
  Future<void> openSettings(LocationAccess access) async => settingsOpened++;
}

class FakeSelfie implements SelfieService {
  FakeSelfie([this.path]);
  String? path;
  int captures = 0;
  final List<String> discarded = [];
  @override
  Future<void> discard(String path) async => discarded.add(path);
  @override
  Future<void> purgeAll() async {}
  @override
  Future<String?> capture() async {
    captures++;
    return path;
  }
}

class FakeAttendanceRepo extends AttendanceRepository {
  FakeAttendanceRepo() : super(Dio(), MemoryJsonCache());

  AttendanceConfig cfg = const AttendanceConfig(
    officeLat: 12.97,
    officeLng: 77.59,
    radiusM: 150,
  );
  AttendanceRecord? todayRecord;
  ApiException? checkInError;
  ApiException? uploadError;
  ApiException? historyError;
  List<AttendanceRecord> historyItems = const [];
  List<CalendarDay> calendarDays = const [];
  final List<String?> checkInSelfieIds = [];
  final List<LocationFix> punches = [];
  int checkOuts = 0;
  int uploads = 0;

  @override
  Future<AttendanceConfig> config() async => cfg;
  @override
  Future<AttendanceRecord?> today() async => todayRecord;

  @override
  Future<AttendanceRecord> checkIn(LocationFix fix, {String? selfieFileId}) async {
    checkInSelfieIds.add(selfieFileId);
    punches.add(fix);
    final e = checkInError;
    if (e != null) throw e;
    return todayRecord = AttendanceRecord(
      id: 'r1',
      workDate: '2026-10-21',
      status: DayStatus.present,
      checkInAt: DateTime(2026, 10, 21, 9, 5),
    );
  }

  @override
  Future<AttendanceRecord> checkOut(LocationFix fix) async {
    checkOuts++;
    return todayRecord = AttendanceRecord(
      id: 'r1',
      workDate: '2026-10-21',
      status: DayStatus.present,
      checkInAt: DateTime(2026, 10, 21, 9, 5),
      checkOutAt: DateTime(2026, 10, 21, 18, 30),
    );
  }

  @override
  Future<String> uploadSelfie(String path) async {
    uploads++;
    final e = uploadError;
    if (e != null) throw e;
    return 'file-1';
  }

  @override
  Future<Cached<Paged<AttendanceRecord>>> history({int page = 1}) async {
    final e = historyError;
    if (e != null) throw e;
    return Cached(
      Paged(items: historyItems, page: 1, total: historyItems.length, pageSize: 20),
    );
  }

  @override
  Future<Cached<CalendarMonth>> calendar(String month) async =>
      Cached(CalendarMonth(month: month, days: calendarDays));

  @override
  Future<Cached<AttendanceSummary>> summary(String month) async =>
      Cached(AttendanceSummary(month: month, present: 10, late: 2, totalWorkedMinutes: 4800));
}

class FakeLateRepo extends LateRepository {
  FakeLateRepo() : super(Dio(), MemoryJsonCache());
  List<LateRecord> records = const [];
  ApiException? error;
  LateSummary? sum;

  @override
  Future<Cached<List<LateRecord>>> list(String month) async {
    final e = error;
    if (e != null) throw e;
    return Cached(records);
  }

  @override
  Future<Cached<LateSummary>> summary(String month) async =>
      Cached(sum ?? LateSummary(month: month));
}
