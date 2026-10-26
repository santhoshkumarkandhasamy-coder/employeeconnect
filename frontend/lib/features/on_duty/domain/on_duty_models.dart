import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../../core/utils/geo.dart';
import '../../attendance/domain/attendance_models.dart';

/// docs/plans/13-on-duty-contract.md 14.2 - employee-side models.
/// Employees never receive the radius of a site (contract 14.2, decision 7).

String? _opt(Object? v) => v is String && v.isNotEmpty ? v : null;

/// A site the employee is assigned to.
class DutyLocation {
  const DutyLocation({
    required this.id,
    required this.name,
    required this.lat,
    required this.lng,
    this.address,
  });
  final String id;
  final String name;
  final String? address;
  final double lat;
  final double lng;

  factory DutyLocation.fromJson(Map<String, dynamic> j) => DutyLocation(
        id: '${j['id'] ?? ''}',
        name: '${j['name'] ?? ''}',
        address: _opt(j['address']),
        lat: (j['lat'] as num?)?.toDouble() ?? 0,
        lng: (j['lng'] as num?)?.toDouble() ?? 0,
      );

  static List<DutyLocation> parseList(Object? json) {
    final raw = json is Map ? json['items'] : null;
    return [
      if (raw is List)
        for (final e in raw)
          if (e is Map<String, dynamic>) DutyLocation.fromJson(e),
    ];
  }
}

/// One visit (check-in at a site, optional check-out).
class DutyVisit {
  const DutyVisit({
    required this.id,
    required this.workDate,
    required this.locationId,
    required this.locationName,
    this.checkInAt,
    this.checkOutAt,
    this.visitMinutes,
    this.open = false,
    this.autoClosed = false,
    this.note,
    this.requestId,
  });
  final String id;
  final String workDate;
  final String locationId;
  final String locationName;
  final DateTime? checkInAt;
  final DateTime? checkOutAt;
  final int? visitMinutes;
  final bool open;
  final bool autoClosed;
  final String? note;
  final String? requestId;

  factory DutyVisit.fromJson(Map<String, dynamic> j) {
    final loc = j['location'];
    final out = parseTimestamp(j['check_out_at']);
    final auto = j['auto_closed'] == true;
    return DutyVisit(
      id: '${j['id'] ?? ''}',
      workDate: '${j['work_date'] ?? ''}',
      locationId: loc is Map ? '${loc['id'] ?? ''}' : '',
      locationName: loc is Map ? '${loc['name'] ?? ''}' : '',
      checkInAt: parseTimestamp(j['check_in_at']),
      checkOutAt: out,
      visitMinutes: (j['visit_minutes'] as num?)?.toInt(),
      // Trust the server flag; derive it only when absent.
      open: j['open'] is bool ? j['open'] as bool : (out == null && !auto),
      autoClosed: auto,
      note: _opt(j['note']),
      requestId: _opt(j['request_id']),
    );
  }

  /// `09:10 - 10:45 (1h 35m)`, `09:10 - open`, `09:10 - no check-out recorded`.
  String get timeText {
    final i = checkInAt == null ? '--:--' : formatTime(checkInAt!);
    if (checkOutAt != null) {
      final d = visitMinutes == null ? '' : ' (${formatDuration(visitMinutes!)})';
      return '$i - ${formatTime(checkOutAt!)}$d';
    }
    return autoClosed ? '$i - no check-out recorded' : '$i - open';
  }
}

/// `approval` block of `GET /on-duty/today`.
class OnDutyApproval {
  const OnDutyApproval({
    this.required = false,
    this.covered = false,
    this.locationIds = const [],
    this.anyLocation = false,
  });
  final bool required;
  final bool covered;
  final List<String> locationIds;
  final bool anyLocation;

  /// Approval is needed and none covers [locationId] today.
  bool blocks(String locationId) =>
      required && !(anyLocation || locationIds.contains(locationId));

  factory OnDutyApproval.fromJson(Object? v) {
    if (v is! Map) return const OnDutyApproval();
    final ids = v['location_ids'];
    return OnDutyApproval(
      required: v['required'] == true,
      covered: v['covered'] == true,
      locationIds: [
        if (ids is List)
          for (final e in ids)
            if (e is String) e,
      ],
      anyLocation: v['any_location'] == true,
    );
  }
}

/// `GET /on-duty/today`.
class OnDutyToday {
  const OnDutyToday({
    this.workDate = '',
    this.enabled = true,
    this.attendance,
    this.openVisit,
    this.visits = const [],
    this.visitsRemaining = 0,
    this.approval = const OnDutyApproval(),
    this.canCheckIn = false,
    this.blockedReason,
  });
  final String workDate;
  final bool enabled;
  final AttendanceRecord? attendance;
  final DutyVisit? openVisit;
  final List<DutyVisit> visits;
  final int visitsRemaining;
  final OnDutyApproval approval;
  final bool canCheckIn;
  final String? blockedReason;

  factory OnDutyToday.fromJson(Map<String, dynamic> j) {
    final att = j['attendance'];
    final open = j['open_visit'];
    final visits = j['visits'];
    return OnDutyToday(
      workDate: '${j['work_date'] ?? ''}',
      enabled: j['enabled'] != false,
      attendance: att is Map<String, dynamic> ? AttendanceRecord.fromJson(att) : null,
      openVisit: open is Map<String, dynamic> ? DutyVisit.fromJson(open) : null,
      visits: [
        if (visits is List)
          for (final e in visits)
            if (e is Map<String, dynamic>) DutyVisit.fromJson(e),
      ],
      visitsRemaining: (j['visits_remaining'] as num?)?.toInt() ?? 0,
      approval: OnDutyApproval.fromJson(j['approval']),
      canCheckIn: j['can_check_in'] == true,
      blockedReason: _opt(j['blocked_reason']),
    );
  }
}

/// Result of a visit check-in / check-out.
class VisitResult {
  const VisitResult({required this.visit, this.attendance});
  final DutyVisit visit;
  final AttendanceRecord? attendance;

  factory VisitResult.fromJson(Map<String, dynamic> j) {
    final att = j['attendance'];
    return VisitResult(
      visit: DutyVisit.fromJson((j['visit'] as Map?)?.cast<String, dynamic>() ?? const {}),
      attendance: att is Map<String, dynamic> ? AttendanceRecord.fromJson(att) : null,
    );
  }
}

// ---- pure helpers -------------------------------------------------------------

/// Within this many metres a site gets the "Nearby" badge. The client does not
/// know the real radius, so this is only a hint; the server decides.
const double nearbyHintM = 150;

class SiteDistance {
  const SiteDistance(this.location, this.meters);
  final DutyLocation location;

  /// Null when there is no fix yet.
  final double? meters;

  bool get nearby => meters != null && meters! <= nearbyHintM;
}

/// Sites sorted nearest first. Without a fix the order is by name.
List<SiteDistance> sortByDistance(List<DutyLocation> sites, {double? lat, double? lng}) {
  final list = [
    for (final s in sites)
      SiteDistance(s, lat == null || lng == null ? null : haversineMeters(lat, lng, s.lat, s.lng)),
  ];
  list.sort((a, b) {
    if (a.meters != null && b.meters != null) {
      final c = a.meters!.compareTo(b.meters!);
      if (c != 0) return c;
    } else if (a.meters != null) {
      return -1;
    } else if (b.meters != null) {
      return 1;
    }
    return a.location.name.toLowerCase().compareTo(b.location.name.toLowerCase());
  });
  return list;
}

/// `90 m`, `1.2 km`, `14 km`. Coarse on purpose (rounded to 10 m under 1 km).
String formatDistance(double meters) {
  if (meters < 1000) {
    final rounded = (meters / 10).round() * 10;
    return '${rounded < 10 ? 10 : rounded} m';
  }
  final km = meters / 1000;
  return km < 10 ? '${km.toStringAsFixed(1)} km' : '${km.round()} km';
}

/// Text for `blocked_reason` (advisory; the server re-checks).
String? blockedReasonText(String? reason) => switch (reason) {
      null => null,
      'ON_DUTY_DISABLED' => 'On Duty is turned off for your company.',
      'NO_SHIFT_ASSIGNED' => 'No shift is assigned to you yet. Please contact HR.',
      'NO_LOCATIONS' => 'No on-duty locations are assigned to you.',
      'ON_LEAVE' => 'You are on approved leave today, so you cannot check in.',
      'VISIT_OPEN' => 'You already have an open visit. Check out of it first.',
      'MAX_VISITS_REACHED' => 'You have reached the visit limit for today.',
      'ON_DUTY_NOT_APPROVED' => 'Approval needed. Request On Duty for today to check in.',
      _ => 'You cannot check in right now.',
    };

/// User-facing text for On Duty domain errors. [locationName] names the site
/// for `OUTSIDE_DUTY_LOCATION`. Never shows a distance or radius.
String onDutyErrorMessage(ApiException e, {String? locationName}) {
  switch (e.code) {
    case 'OUTSIDE_DUTY_LOCATION':
      return locationName == null || locationName.isEmpty
          ? 'You are outside the site. Move closer and try again.'
          : 'You are outside $locationName. Move closer and try again.';
    case 'VISIT_OPEN':
      return 'You already have an open visit. Check out of it first.';
    case 'NO_OPEN_VISIT':
      return 'You have no open visit to check out of.';
    case 'MAX_VISITS_REACHED':
      return 'You have reached the visit limit for today.';
    case 'ON_DUTY_NOT_APPROVED':
      return 'Approval needed. Request On Duty for today, then check in once it is approved.';
    case 'DUTY_LOCATION_UNAVAILABLE':
      return 'That location is not available to you any more. The list was refreshed.';
    case 'ON_DUTY_DISABLED':
      return 'On Duty is turned off.';
    default:
      return attendanceErrorMessage(e);
  }
}

/// Codes after which the visible state is stale and must be reloaded.
bool onDutyNeedsReload(ApiException e) => const {
      'VISIT_OPEN',
      'NO_OPEN_VISIT',
      'MAX_VISITS_REACHED',
      'ON_DUTY_NOT_APPROVED',
      'DUTY_LOCATION_UNAVAILABLE',
      'ON_DUTY_DISABLED',
      'ON_LEAVE',
      'ALREADY_CHECKED_IN',
    }.contains(e.code);

const int maxVisitNoteLength = 200;

String? validateVisitNote(String? v) {
  if ((v ?? '').trim().length > maxVisitNoteLength) {
    return 'At most $maxVisitNoteLength characters';
  }
  return null;
}

/// Note to send: trimmed, null when blank.
String? cleanNote(String? v) {
  final t = (v ?? '').trim();
  return t.isEmpty ? null : t;
}

// ---- coordinate parsing ---------------------------------------------------------

typedef LatLng = ({double lat, double lng});

bool validLatLng(double lat, double lng) =>
    lat.isFinite && lng.isFinite && lat >= -90 && lat <= 90 && lng >= -180 && lng <= 180;

const String _n = r'(-?\d{1,3}(?:\.\d+)?)';

final List<RegExp> _coordPatterns = <RegExp>[
  RegExp('^$_n\\s*[,;]\\s*$_n\$'),
  RegExp('^$_n\\s+$_n\$'),
  RegExp('!3d$_n!4d$_n'),
  RegExp('@$_n,$_n'),
  RegExp('[?&](?:q|ll|query|center|destination)=$_n(?:,|%2C)$_n', caseSensitive: false),
];

/// Reads coordinates typed or pasted by an admin: `12.97, 77.59`, `12.97 77.59`,
/// or a (long) maps link containing `@lat,lng`, `!3dLAT!4dLNG` or
/// `?q=lat,lng` / `ll=lat,lng`. Returns null when nothing valid is found
/// (short links such as maps.app.goo.gl hide the coordinates and cannot be read).
LatLng? parseCoordinates(String? input) {
  final s = (input ?? '').trim();
  if (s.isEmpty || s.length > 2000) return null;
  for (final re in _coordPatterns) {
    final m = re.firstMatch(s);
    if (m == null) continue;
    final lat = double.tryParse(m.group(1) ?? '');
    final lng = double.tryParse(m.group(2) ?? '');
    if (lat != null && lng != null && validLatLng(lat, lng)) return (lat: lat, lng: lng);
  }
  return null;
}

/// Rounds to 6 decimals (about 0.1 m) for display and sending.
double roundCoord(double v) => (v * 1e6).round() / 1e6;

/// Maps-app link for a site (coordinates of the SITE only, never the user).
String mapsLinkFor(double lat, double lng) =>
    'https://www.google.com/maps/search/?api=1&query=${roundCoord(lat)},${roundCoord(lng)}';
