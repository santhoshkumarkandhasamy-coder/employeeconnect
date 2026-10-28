import '../../../core/utils/dates.dart';
import 'admin_models.dart' show EmployeeRef;

/// api-contract.md section 12 (company, license, sessions, console data).

String? _opt(Object? v) => v is String && v.isNotEmpty ? v : null;
int _int(Object? v) => (v as num?)?.toInt() ?? 0;

// ---- offboarding -----------------------------------------------------------------

enum DeactivationReason {
  resigned('resigned', 'Resigned'),
  terminated('terminated', 'Terminated'),
  contractEnd('contract_end', 'Contract ended'),
  absconded('absconded', 'Absconded'),
  other('other', 'Other');

  const DeactivationReason(this.code, this.label);
  final String code;
  final String label;

  static String labelFor(String? code) {
    for (final r in values) {
      if (r.code == code) return r.label;
    }
    return code ?? '-';
  }
}

// ---- company -----------------------------------------------------------------------

/// `GET/PUT /admin/company`.
class CompanyAdmin {
  const CompanyAdmin({
    this.name,
    this.logoUrl,
    this.logoVersion = 0,
    this.configured = false,
    this.deploymentId = '',
    this.setupCompletedAt,
  });
  final String? name;
  final String? logoUrl;
  final int logoVersion;
  final bool configured;
  final String deploymentId;
  final DateTime? setupCompletedAt;

  factory CompanyAdmin.fromJson(Map<String, dynamic> j) => CompanyAdmin(
        name: _opt(j['name']),
        logoUrl: _opt(j['logo_url']),
        logoVersion: _int(j['logo_version']),
        configured: j['configured'] == true,
        deploymentId: '${j['deployment_id'] ?? ''}',
        setupCompletedAt: parseTimestamp(j['setup_completed_at']),
      );
}

/// Company name rule: 2-100 characters after trimming.
String? validateCompanyName(String? v) {
  final t = (v ?? '').trim();
  if (t.length < 2) return 'Enter at least 2 characters';
  if (t.length > 100) return 'At most 100 characters';
  return null;
}

/// Logo rule: png or jpeg, at most 2 MB (the server re-checks by magic bytes).
String? validateLogoFile(String name, int bytes) {
  final n = name.toLowerCase();
  if (!(n.endsWith('.png') || n.endsWith('.jpg') || n.endsWith('.jpeg'))) {
    return 'Choose a PNG or JPEG image';
  }
  if (bytes > 2 * 1024 * 1024) return 'The image must be 2 MB or smaller';
  return null;
}

// ---- license -----------------------------------------------------------------------

class LicenseInfo {
  const LicenseInfo({
    required this.status,
    required this.maxUsers,
    required this.activeUsers,
    required this.seatsAvailable,
    required this.onExpiry,
    required this.deploymentId,
    this.licenseId,
    this.licensedTo,
    this.issuedAt,
    this.expiresAt,
    this.graceUntil,
  });
  final String status;
  final String? licenseId;
  final String? licensedTo;
  final int maxUsers;
  final int activeUsers;
  final int seatsAvailable;
  final DateTime? issuedAt;
  final DateTime? expiresAt;
  final DateTime? graceUntil;
  final String onExpiry;
  final String deploymentId;

  bool get isGrace => status == 'grace';
  bool get isExpired => status == 'expired';
  bool get isFull => seatsAvailable <= 0;

  factory LicenseInfo.fromJson(Map<String, dynamic> j) => LicenseInfo(
        status: '${j['status'] ?? 'unlicensed'}',
        licenseId: _opt(j['license_id']),
        licensedTo: _opt(j['licensed_to']),
        maxUsers: _int(j['max_users']),
        activeUsers: _int(j['active_users']),
        seatsAvailable: _int(j['seats_available']),
        issuedAt: parseTimestamp(j['issued_at']),
        expiresAt: parseTimestamp(j['expires_at']),
        graceUntil: parseTimestamp(j['grace_until']),
        onExpiry: '${j['on_expiry'] ?? 'block_new_users'}',
        deploymentId: '${j['deployment_id'] ?? ''}',
      );
}

String licenseStatusLabel(String status) => switch (status) {
      'valid' => 'Valid',
      'grace' => 'Grace period',
      'expired' => 'Expired',
      'invalid' => 'Invalid (treated as unlicensed)',
      _ => 'Unlicensed',
    };

/// Banner text for a license that needs attention, or null.
String? licenseBanner(LicenseInfo l) {
  if (l.isExpired) {
    return l.onExpiry == 'read_only'
        ? 'The license has expired. The system is read-only until a new license is installed.'
        : 'The license has expired. New employees cannot be added or activated.';
  }
  if (l.isGrace) {
    final until = l.graceUntil;
    return 'The license has expired and is in its grace period'
        '${until == null ? '' : ' until ${ymd(until)}'}. Install a new license.';
  }
  if (l.status == 'invalid') return 'The stored license is not valid for this installation.';
  if (l.activeUsers > l.maxUsers) {
    return 'More employees are active (${l.activeUsers}) than licensed (${l.maxUsers}). '
        'No new employees can be added.';
  }
  return null;
}

/// Format check only (the server verifies the signature): `ECL1.<p>.<sig>`.
String? validateLicenseKey(String? v) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return 'Paste the license key';
  final parts = t.split('.');
  if (parts.length != 3 || parts[0] != 'ECL1' || parts[1].isEmpty || parts[2].isEmpty) {
    return 'This is not a license key (expected ECL1.…)';
  }
  if (t.length > 4096) return 'The key is too long';
  if (t.contains(RegExp(r'\s'))) return 'Remove spaces and line breaks';
  return null;
}

/// Seats hint for the employee create form.
String seatsHint(LicenseInfo l) => l.seatsAvailable <= 0
    ? 'No seats left (${l.activeUsers} of ${l.maxUsers} used).'
    : '${l.seatsAvailable} seats left (${l.activeUsers} of ${l.maxUsers} used).';

// ---- sessions ----------------------------------------------------------------------

class SessionItem {
  const SessionItem({
    required this.id,
    required this.clientType,
    this.user,
    this.platform,
    this.deviceName,
    this.appVersion,
    this.ip,
    this.startedAt,
    this.lastSeenAt,
    this.expiresAt,
  });
  final String id;
  final EmployeeRef? user;
  final String clientType;
  final String? platform;
  final String? deviceName;
  final String? appVersion;
  final String? ip;
  final DateTime? startedAt;
  final DateTime? lastSeenAt;
  final DateTime? expiresAt;

  bool get isWeb => clientType == 'web';

  String get deviceLabel {
    final d = deviceName ?? platform ?? clientType;
    return appVersion == null ? d : '$d, v$appVersion';
  }

  factory SessionItem.fromJson(Map<String, dynamic> j) => SessionItem(
        id: '${j['id'] ?? ''}',
        user: EmployeeRef.parse(j['user']),
        clientType: '${j['client_type'] ?? 'mobile'}',
        platform: _opt(j['platform']),
        deviceName: _opt(j['device_name']),
        appVersion: _opt(j['app_version']),
        ip: _opt(j['ip']),
        startedAt: parseTimestamp(j['started_at']),
        lastSeenAt: parseTimestamp(j['last_seen_at']),
        expiresAt: parseTimestamp(j['expires_at']),
      );
}

// ---- console data ------------------------------------------------------------------

class Dashboard {
  const Dashboard({
    this.date,
    this.employees,
    this.attendance,
    this.requests,
    this.sessions,
    this.resetOpen,
    this.seats,
  });
  final String? date;
  final Map<String, int>? employees;
  final Map<String, int>? attendance;
  final Map<String, int>? requests;
  final Map<String, int>? sessions;
  final int? resetOpen;
  final DashboardSeats? seats;

  static Map<String, int>? _counts(Object? v) => v is Map
      ? {for (final e in v.entries) '${e.key}': (e.value is num) ? (e.value as num).toInt() : 0}
      : null;

  int? get pendingRequests {
    final r = requests;
    return r == null
        ? null
        : (r['leave_pending'] ?? 0) + (r['permission_pending'] ?? 0) + (r['on_duty_pending'] ?? 0);
  }

  factory Dashboard.fromJson(Map<String, dynamic> j) {
    final seats = j['seats'];
    return Dashboard(
      date: _opt(j['date']),
      employees: _counts(j['employees']),
      attendance: _counts(j['attendance']),
      requests: _counts(j['requests']),
      sessions: _counts(j['sessions']),
      resetOpen: (j['password_reset_requests_open'] as num?)?.toInt(),
      seats: seats is Map<String, dynamic> ? DashboardSeats.fromJson(seats) : null,
    );
  }
}

class DashboardSeats {
  const DashboardSeats({required this.used, required this.total, required this.status});
  final int used;
  final int total;
  final String status;
  factory DashboardSeats.fromJson(Map<String, dynamic> j) => DashboardSeats(
        used: _int(j['used']),
        total: _int(j['total']),
        status: '${j['status'] ?? 'unlicensed'}',
      );
}

class AttendanceDayRow {
  const AttendanceDayRow({
    required this.user,
    required this.status,
    this.checkInAt,
    this.checkOutAt,
    this.minutesLate = 0,
    this.leaveType,
  });
  final EmployeeRef? user;
  final String status;
  final DateTime? checkInAt;
  final DateTime? checkOutAt;
  final int minutesLate;
  final String? leaveType;

  factory AttendanceDayRow.fromJson(Map<String, dynamic> j) => AttendanceDayRow(
        user: EmployeeRef.parse(j['user']),
        status: '${j['status'] ?? ''}',
        checkInAt: parseTimestamp(j['check_in_at']),
        checkOutAt: parseTimestamp(j['check_out_at']),
        minutesLate: _int(j['minutes_late']),
        leaveType: _opt(j['leave_type']),
      );
}

String attendanceStatusLabel(String s) => switch (s) {
      'present' => 'Present',
      'late' => 'Late',
      'half_day' => 'Half day',
      'not_checked_in' => 'Not checked in',
      'on_leave' => 'On leave',
      'holiday' => 'Holiday',
      'weekly_off' => 'Weekly off',
      _ => s,
    };

class SystemHealth {
  const SystemHealth({
    required this.status,
    required this.checks,
    this.appVersion,
    this.tlsDaysLeft,
    this.uploadsFreePct,
  });
  final String status;
  final String? appVersion;
  final int? tlsDaysLeft;
  final int? uploadsFreePct;

  /// Display name -> ok.
  final Map<String, bool> checks;

  bool get degraded => status != 'ok';

  factory SystemHealth.fromJson(Map<String, dynamic> j) {
    bool ok(Object? v) => v is Map && v['ok'] == true;
    final tls = j['tls'];
    final storage = j['storage'];
    final push = j['push'];
    return SystemHealth(
      status: '${j['status'] ?? 'degraded'}',
      appVersion: _opt(j['app_version']),
      tlsDaysLeft: tls is Map ? (tls['days_left'] as num?)?.toInt() : null,
      uploadsFreePct: storage is Map ? (storage['uploads_free_pct'] as num?)?.toInt() : null,
      checks: {
        'Database': ok(j['db']),
        'Redis': ok(j['redis']),
        'Worker': ok(j['worker']),
        'Backup': ok(j['backup']),
        if (push is Map) 'Push configured': push['configured'] == true,
      },
    );
  }
}
