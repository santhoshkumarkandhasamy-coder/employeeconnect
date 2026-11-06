import '../../../core/utils/dates.dart';
import '../../admin/domain/admin_models.dart' show AdminPerm, EmployeeRef;
import 'device_errors.dart';

export 'device_errors.dart' show deviceErrorText, deviceJobErrorText;

/// api-contract.md section 21 (attendance devices). Nothing here is cached.

String _s(Object? v) => v == null ? '' : '$v';
String? _opt(Object? v) => v is String && v.isNotEmpty ? v : null;
int? _intOrNull(Object? v) => (v as num?)?.toInt();

// ---- permissions -----------------------------------------------------------------

/// The Devices area (list, unmatched users, punch log) needs `device.view`.
bool canViewDevices(List<String> p) => p.contains(AdminPerm.deviceView);

/// Map / ignore / reset device users.
bool canMapDevices(List<String> p) => p.contains(AdminPerm.deviceMap);

/// Add, edit, delete, test and accept a new identity.
bool canManageDevices(List<String> p) => p.contains(AdminPerm.deviceManage);

/// Sync now: `device.map` or `device.manage`.
bool canSyncDevices(List<String> p) => canMapDevices(p) || canManageDevices(p);

// ---- device ----------------------------------------------------------------------

class DeviceRef {
  const DeviceRef({required this.id, required this.name});
  final String id;
  final String name;

  static DeviceRef? parse(Object? v) => v is Map ? DeviceRef(id: _s(v['id']), name: _s(v['name'])) : null;
}

enum DeviceStatus {
  ok('OK'),
  failing('Failing'),
  offline('Offline'),
  disabled('Disabled'),
  neverSynced('Never synced');

  const DeviceStatus(this.label);
  final String label;

  static DeviceStatus parse(Object? v) => switch (v) {
        'failing' => failing,
        'offline' => offline,
        'disabled' => disabled,
        'never_synced' => neverSynced,
        _ => ok,
      };
}

class Device {
  const Device({
    required this.id,
    required this.name,
    required this.ip,
    required this.port,
    required this.transport,
    required this.status,
    this.locationLabel,
    this.hasCommKey = false,
    this.enabled = true,
    this.timezone,
    this.clockCorrection = 'auto',
    this.ingestFrom,
    this.serialNumber,
    this.model,
    this.firmware,
    this.clockStatus,
    this.lastClockOffsetS,
    this.lastSyncAt,
    this.lastSuccessAt,
    this.lastErrorCode,
    this.lastErrorAt,
    this.circuitOpenUntil,
    this.recordsOnDevice,
    this.usersOnDevice,
    this.unmatchedUsers = 0,
    this.recentSyncs = const [],
  });

  final String id;
  final String name;
  final String? locationLabel;
  final String ip;
  final int port;
  final String transport;
  final bool hasCommKey;
  final bool enabled;
  final String? timezone;
  final String clockCorrection;
  final String? ingestFrom;
  final String? serialNumber;
  final String? model;
  final String? firmware;
  final DeviceStatus status;
  final String? clockStatus;
  final int? lastClockOffsetS;
  final DateTime? lastSyncAt;
  final DateTime? lastSuccessAt;
  final String? lastErrorCode;
  final DateTime? lastErrorAt;
  final DateTime? circuitOpenUntil;
  final int? recordsOnDevice;
  final int? usersOnDevice;
  final int unmatchedUsers;
  final List<DeviceSync> recentSyncs;

  bool get clockDrift => clockStatus == 'drift' || clockStatus == 'invalid';
  String get address => '$ip:$port';

  factory Device.fromJson(Map<String, dynamic> j) => Device(
        id: _s(j['id']),
        name: _s(j['name']),
        locationLabel: _opt(j['location_label']),
        ip: _s(j['ip']),
        port: _intOrNull(j['port']) ?? 4370,
        transport: _s(j['transport']).isEmpty ? 'tcp' : _s(j['transport']),
        hasCommKey: j['has_comm_key'] == true,
        enabled: j['enabled'] != false,
        timezone: _opt(j['timezone']),
        clockCorrection: _s(j['clock_correction']).isEmpty ? 'auto' : _s(j['clock_correction']),
        ingestFrom: _opt(j['ingest_from']),
        serialNumber: _opt(j['serial_number']),
        model: _opt(j['model']),
        firmware: _opt(j['firmware']),
        status: DeviceStatus.parse(j['status']),
        clockStatus: _opt(j['clock_status']),
        lastClockOffsetS: _intOrNull(j['last_clock_offset_s']),
        lastSyncAt: parseTimestamp(j['last_sync_at']),
        lastSuccessAt: parseTimestamp(j['last_success_at']),
        lastErrorCode: _opt(j['last_error_code']),
        lastErrorAt: parseTimestamp(j['last_error_at']),
        circuitOpenUntil: parseTimestamp(j['circuit_open_until']),
        recordsOnDevice: _intOrNull(j['records_on_device']),
        usersOnDevice: _intOrNull(j['users_on_device']),
        unmatchedUsers: _intOrNull(j['unmatched_users']) ?? 0,
        recentSyncs: [
          if (j['recent_syncs'] is List)
            for (final s in j['recent_syncs'] as List)
              if (s is Map<String, dynamic>) DeviceSync.fromJson(s),
        ],
      );
}

/// "5 min ago", "2 h ago", "3 d ago", or "never".
String relativeTime(DateTime? t, {DateTime? now}) {
  if (t == null) return 'never';
  final d = (now ?? DateTime.now()).difference(t);
  if (d.inSeconds < 60) return 'just now';
  if (d.inMinutes < 60) return '${d.inMinutes} min ago';
  if (d.inHours < 24) return '${d.inHours} h ago';
  return '${d.inDays} d ago';
}

/// Status text for the list chip; clock drift is reported next to an otherwise healthy device.
String deviceStatusLabel(Device d) {
  if (d.status == DeviceStatus.ok && d.clockDrift) {
    return d.clockStatus == 'invalid' ? 'Clock wrong' : 'Clock drift';
  }
  if (d.status == DeviceStatus.offline) return 'Offline (paused)';
  return d.status.label;
}

/// "+14 s" / "-2 min" style text for a clock offset.
String clockOffsetText(int? seconds) {
  if (seconds == null) return '-';
  final sign = seconds < 0 ? '-' : '+';
  final a = seconds.abs();
  if (a < 120) return '$sign$a s';
  if (a < 7200) return '$sign${a ~/ 60} min';
  return '$sign${a ~/ 3600} h';
}

// ---- sync jobs ---------------------------------------------------------------------

class DeviceSyncResult {
  const DeviceSyncResult({
    this.serialNumber,
    this.model,
    this.firmware,
    this.platform,
    this.deviceTime,
    this.clockOffsetS,
    this.usersOnDevice,
    this.recordsOnDevice,
  });
  final String? serialNumber;
  final String? model;
  final String? firmware;
  final String? platform;
  final String? deviceTime;
  final int? clockOffsetS;
  final int? usersOnDevice;
  final int? recordsOnDevice;

  static DeviceSyncResult? parse(Object? v) => v is Map
      ? DeviceSyncResult(
          serialNumber: _opt(v['serial_number']),
          model: _opt(v['model']),
          firmware: _opt(v['firmware']),
          platform: _opt(v['platform']),
          deviceTime: _opt(v['device_time']),
          clockOffsetS: _intOrNull(v['clock_offset_s']),
          usersOnDevice: _intOrNull(v['users_on_device']),
          recordsOnDevice: _intOrNull(v['records_on_device']),
        )
      : null;
}

class DeviceSync {
  const DeviceSync({
    required this.id,
    required this.kind,
    required this.status,
    this.deviceId,
    this.errorCode,
    this.errorMessage,
    this.queuedAt,
    this.startedAt,
    this.finishedAt,
    this.fetched,
    this.inserted,
    this.quarantined,
    this.daysRecomputed,
    this.result,
  });
  final String id;
  final String? deviceId;
  final String kind;
  final String status;
  final String? errorCode;
  final String? errorMessage;
  final DateTime? queuedAt;
  final DateTime? startedAt;
  final DateTime? finishedAt;
  final int? fetched;
  final int? inserted;
  final int? quarantined;
  final int? daysRecomputed;
  final DeviceSyncResult? result;

  /// queued / running are still in progress; everything else is final.
  bool get isFinished => status != 'queued' && status != 'running';
  bool get succeeded => status == 'ok';

  /// What the user sees for a finished job.
  String get summary {
    if (succeeded) {
      if (kind == 'test') return 'Connection works.';
      return 'Synced: ${fetched ?? 0} records read, ${inserted ?? 0} new'
          '${(quarantined ?? 0) > 0 ? ', $quarantined set aside' : ''}.';
    }
    return deviceJobErrorText(errorCode, errorMessage);
  }

  factory DeviceSync.fromJson(Map<String, dynamic> j) => DeviceSync(
        id: _s(j['id']),
        deviceId: _opt(j['device_id']),
        kind: _s(j['kind']),
        status: _s(j['status']),
        errorCode: _opt(j['error_code']),
        errorMessage: _opt(j['error_message']),
        queuedAt: parseTimestamp(j['queued_at']),
        startedAt: parseTimestamp(j['started_at']),
        finishedAt: parseTimestamp(j['finished_at']),
        fetched: _intOrNull(j['fetched']),
        inserted: _intOrNull(j['inserted']),
        quarantined: _intOrNull(j['quarantined']),
        daysRecomputed: _intOrNull(j['days_recomputed']),
        result: DeviceSyncResult.parse(j['result']),
      );
}

// ---- device form -----------------------------------------------------------------

/// What to do with the write-only communication key on a save.
enum CommKeyAction { keep, set, clear }

String? validateDeviceName(String? v) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return 'Enter a name';
  if (t.length > 60) return 'At most 60 characters';
  return null;
}

String? validateDeviceIp(String? v) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return 'Enter the IP address';
  final m = RegExp(r'^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$').firstMatch(t);
  if (m == null) return 'Use an address like 192.168.0.201';
  for (var i = 1; i <= 4; i++) {
    if (int.parse(m[i]!) > 255) return 'Each part must be 0 to 255';
  }
  return null;
}

String? validateDevicePort(String? v) {
  final n = int.tryParse((v ?? '').trim());
  if (n == null) return 'Enter a port number';
  if (n < 1 || n > 65535) return 'Port must be 1 to 65535';
  return null;
}

String? validateCommKey(String? v) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return 'Enter the key, or choose another option';
  if (!RegExp(r'^\d{1,6}$').hasMatch(t)) return 'The key is a number up to 999999';
  return null;
}

String? validateDeviceLocation(String? v) => (v ?? '').trim().length > 80 ? 'At most 80 characters' : null;

/// Empty = office timezone. Otherwise an IANA name such as Asia/Kolkata.
String? validateDeviceTimezone(String? v) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return null;
  if (t == 'UTC' || RegExp(r'^[A-Za-z_]+(/[A-Za-z0-9_+\-]+){1,2}$').hasMatch(t)) return null;
  return 'Use a name like Asia/Kolkata, or leave empty for the office timezone';
}

/// `ingest_from` may be at most 31 days back and not in the future.
String? validateIngestFrom(DateTime? d, DateTime now) {
  if (d == null) return 'Choose the date';
  final today = DateTime(now.year, now.month, now.day);
  final day = DateTime(d.year, d.month, d.day);
  if (day.isAfter(today)) return 'The date cannot be in the future';
  if (today.difference(day).inDays > 31) return 'At most 31 days back';
  return null;
}

/// Request body of POST / PUT /admin/devices. [commKey] only matters for [CommKeyAction.set].
/// On create `keep` and `clear` both send null; on edit `keep` omits the key (unchanged).
Map<String, Object?> deviceBody({
  required String name,
  required String ip,
  required String port,
  required String transport,
  required CommKeyAction keyAction,
  required String commKey,
  required bool enabled,
  required String location,
  required String timezone,
  required DateTime ingestFrom,
  required bool create,
}) {
  final loc = location.trim();
  final tz = timezone.trim();
  return {
    'name': name.trim(),
    'ip': ip.trim(),
    'port': int.parse(port.trim()),
    'transport': transport,
    'enabled': enabled,
    'location_label': loc.isEmpty ? null : loc,
    'timezone': tz.isEmpty ? null : tz,
    'clock_correction': 'auto',
    'ingest_from': ymd(ingestFrom),
    if (keyAction == CommKeyAction.set)
      'comm_key': int.parse(commKey.trim())
    else if (keyAction == CommKeyAction.clear || create)
      'comm_key': null,
  };
}

// ---- device users ------------------------------------------------------------------

class DeviceUser {
  const DeviceUser({
    required this.id,
    required this.deviceUserId,
    required this.status,
    this.device,
    this.name,
    this.cardMasked,
    this.isDeviceAdmin = false,
    this.presentOnDevice = true,
    this.matchMethod = 'unmatched',
    this.user,
    this.lastPunchAt,
    this.unappliedPunches = 0,
  });
  final String id;
  final DeviceRef? device;
  final String deviceUserId;
  final String? name;
  final String? cardMasked;
  final bool isDeviceAdmin;
  final bool presentOnDevice;
  final String status; // matched | unmatched | ignored
  final String matchMethod;
  final EmployeeRef? user;
  final DateTime? lastPunchAt;
  final int unappliedPunches;

  bool get isMatched => status == 'matched';
  bool get isIgnored => status == 'ignored';

  factory DeviceUser.fromJson(Map<String, dynamic> j) => DeviceUser(
        id: _s(j['id']),
        device: DeviceRef.parse(j['device']),
        deviceUserId: _s(j['device_user_id']),
        name: _opt(j['name']),
        cardMasked: _opt(j['card_masked']),
        isDeviceAdmin: j['is_device_admin'] == true,
        presentOnDevice: j['present_on_device'] != false,
        status: _s(j['status']).isEmpty ? 'unmatched' : _s(j['status']),
        matchMethod: _s(j['match_method']),
        user: EmployeeRef.parse(j['user']),
        lastPunchAt: parseTimestamp(j['last_punch_at']),
        unappliedPunches: _intOrNull(j['unapplied_punches']) ?? 0,
      );
}

/// `PUT .../mapping` and `POST .../auto` answer with every row that changed.
class MappingResult {
  const MappingResult({required this.items, this.recomputeQueued = false});
  final List<DeviceUser> items;
  final bool recomputeQueued;

  factory MappingResult.fromJson(Map<String, dynamic> j) => MappingResult(
        items: [
          if (j['items'] is List)
            for (final e in j['items'] as List)
              if (e is Map<String, dynamic>) DeviceUser.fromJson(e),
        ],
        recomputeQueued: j['recompute_queued'] == true,
      );
}

/// Filter values of the users list.
const Map<String, String> deviceUserStatuses = {
  'unmatched': 'Unmatched',
  'matched': 'Matched',
  'ignored': 'Ignored',
  'all': 'All',
};

/// Device user list filter / provider family key.
class DeviceUserQuery {
  const DeviceUserQuery({this.status = 'unmatched', this.deviceId, this.q = ''});
  final String status;
  final String? deviceId;
  final String q;

  Map<String, Object?> toQuery() => {
        'status': status,
        if (deviceId != null) 'device_id': deviceId,
        if (q.trim().isNotEmpty) 'q': q.trim(),
      };

  @override
  bool operator ==(Object other) =>
      other is DeviceUserQuery && other.status == status && other.deviceId == deviceId && other.q == q;

  @override
  int get hashCode => Object.hash(status, deviceId, q);
}

// ---- punch log ---------------------------------------------------------------------

class DevicePunch {
  const DevicePunch({
    required this.id,
    required this.deviceUserId,
    required this.outcome,
    this.device,
    this.user,
    this.punchAt,
    this.deviceTime,
    this.state = 'unknown',
    this.method = 'other',
    this.workDate,
    this.reason,
  });
  final String id;
  final DeviceRef? device;
  final String deviceUserId;
  final EmployeeRef? user;
  final DateTime? punchAt;
  final String? deviceTime;
  final String state;
  final String method;
  final String? workDate;
  final String outcome;
  final String? reason;

  factory DevicePunch.fromJson(Map<String, dynamic> j) => DevicePunch(
        id: _s(j['id']),
        device: DeviceRef.parse(j['device']),
        deviceUserId: _s(j['device_user_id']),
        user: EmployeeRef.parse(j['user']),
        punchAt: parseTimestamp(j['punch_at']),
        deviceTime: _opt(j['device_time']),
        state: _s(j['state']),
        method: _s(j['method']),
        workDate: _opt(j['work_date']),
        outcome: _s(j['outcome']),
        reason: _opt(j['reason']),
      );
}

const Map<String, String> punchOutcomeLabels = {
  'check_in': 'Used as check-in',
  'check_out': 'Used as check-out',
  'extra': 'Extra punch',
  'unmatched': 'No employee linked',
  'ignored': 'Ignored',
  'quarantined': 'Set aside',
  'not_applied': 'Not applied',
};

String punchOutcomeLabel(String o) => punchOutcomeLabels[o] ?? o;

const Map<String, String> punchReasonLabels = {
  'on_leave': 'employee on leave',
  'on_duty': 'on-duty day',
  'no_shift': 'no shift assigned',
  'inactive_user': 'employee not active',
  'manual_entry': 'HR entered the day manually',
  'outside_window': 'older than the allowed window',
  'payroll_locked': 'payroll already locked',
  'mapping_ignored': 'device user is ignored',
  'clock_invalid': 'machine clock was wrong',
  'future': 'time is in the future',
  'duplicate': 'duplicate of another punch',
};

String punchReasonLabel(String? r) => r == null ? '' : (punchReasonLabels[r] ?? r);

/// Neutral wording for how a punch was made (no technical terms).
String punchMethodLabel(String m) => switch (m) {
      'card' => 'Card',
      'password' => 'Code',
      _ => 'Machine',
    };

/// Outcomes available as a filter, in display order.
const List<String> punchOutcomeFilters = [
  'check_in',
  'check_out',
  'extra',
  'unmatched',
  'ignored',
  'quarantined',
  'not_applied',
];

/// Punch log filter; also the provider family key.
class PunchQuery {
  const PunchQuery({
    required this.from,
    required this.to,
    this.deviceId,
    this.employeeId,
    this.deviceUserId = '',
    this.outcome,
  });
  final String from;
  final String to;
  final String? deviceId;
  final String? employeeId;
  final String deviceUserId;
  final String? outcome;

  Map<String, Object?> toQuery() => {
        'from': from,
        'to': to,
        if (deviceId != null) 'device_id': deviceId,
        if (employeeId != null) 'employee_id': employeeId,
        if (deviceUserId.trim().isNotEmpty) 'device_user_id': deviceUserId.trim(),
        if (outcome != null) 'outcome': outcome,
      };

  @override
  bool operator ==(Object other) =>
      other is PunchQuery &&
      other.from == from &&
      other.to == to &&
      other.deviceId == deviceId &&
      other.employeeId == employeeId &&
      other.deviceUserId == deviceUserId &&
      other.outcome == outcome;

  @override
  int get hashCode => Object.hash(from, to, deviceId, employeeId, deviceUserId, outcome);
}

/// First problem with a punch log date range (max 92 days), or null.
String? validatePunchRange(DateTime from, DateTime to) {
  if (to.isBefore(from)) return 'The start date must be before the end date';
  if (to.difference(from).inDays > 91) return 'Choose a range of at most 92 days';
  return null;
}

// ---- employee view -----------------------------------------------------------------

/// One entry of `GET /attendance/punches` (no ids, cards or device data).
class MyPunch {
  const MyPunch({this.punchAt, this.workDate, this.deviceName = '', this.method = 'other', this.usedAs = 'none'});
  final DateTime? punchAt;
  final String? workDate;
  final String deviceName;
  final String method;
  final String usedAs;

  factory MyPunch.fromJson(Map<String, dynamic> j) => MyPunch(
        punchAt: parseTimestamp(j['punch_at']),
        workDate: _opt(j['work_date']),
        deviceName: _s(j['device_name']),
        method: _s(j['method']),
        usedAs: _s(j['used_as']).isEmpty ? 'none' : _s(j['used_as']),
      );

  String get usedAsLabel => switch (usedAs) {
        'check_in' => 'Counted as check-in',
        'check_out' => 'Counted as check-out',
        _ => 'Not counted',
      };
}

/// Source of an attendance time for the badge: Phone / Device / HR.
String sourceLabel(String? source) => switch (source) {
      'device' => 'Device',
      'manual' => 'HR',
      'app' => 'Phone',
      _ => '',
    };

/// Privacy notice (More -> Privacy). Text is fixed by plan 18 section 8.
const String devicePrivacyNotice =
    'Office attendance machines: Employee Connect receives only your device user number, name, '
    'card number and the time and method of each punch. Fingerprint or face data stays on the '
    'machine and is never copied to Employee Connect.';
