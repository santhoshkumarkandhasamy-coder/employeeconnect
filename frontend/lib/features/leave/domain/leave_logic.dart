import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import 'leave_models.dart';

/// Pure client-side rules for leave / permission forms and error text.
/// The server stays authoritative; this gives instant feedback.

/// Default `leave.backdate_days` (server setting); only used for the picker range.
const int defaultBackdateDays = 7;

/// Default `leave.max_advance_days` (365). `/config` does not expose it, so the
/// picker is capped here; the server stays authoritative (422 INVALID_DATE_RANGE).
const int defaultMaxAdvanceDays = 365;

/// `GET /leave/balance` accepts 2000-2200 only (else 422 VALIDATION_ERROR).
const int minBalanceYear = 2000;
const int maxBalanceYear = 2200;
bool isValidBalanceYear(int y) => y >= minBalanceYear && y <= maxBalanceYear;

/// Last pickable leave date.
DateTime leaveLastDate(DateTime today, {int maxAdvanceDays = defaultMaxAdvanceDays}) =>
    dateOnly(today).add(Duration(days: maxAdvanceDays));

const String mergedRequestsHint =
    'Requests of the same leave type that touch each other (also across weekly offs and holidays) '
    'are counted together for the consecutive-day limit and the attachment rule.';

const String overnightShiftHint =
    'For a night shift, choose the day the shift starts. Hours after midnight belong to that shift.';

/// Seconds text for a Retry-After wait, e.g. `15 seconds`, `1 minute`.
String waitText(Duration d) {
  final s = d.inSeconds < 1 ? 1 : d.inSeconds;
  if (s < 60) return '$s ${s == 1 ? 'second' : 'seconds'}';
  final m = (s / 60).ceil();
  return '$m ${m == 1 ? 'minute' : 'minutes'}';
}

/// Content type of attachment bytes: the declared one when known, else sniffed.
String? sniffContentType(List<int> b, {String? declared}) {
  if (declared == 'image/jpeg' || declared == 'image/png' || declared == 'application/pdf') {
    return declared;
  }
  if (b.length >= 4 && b[0] == 0x25 && b[1] == 0x50 && b[2] == 0x44 && b[3] == 0x46) return 'application/pdf';
  if (b.length >= 3 && b[0] == 0xFF && b[1] == 0xD8 && b[2] == 0xFF) return 'image/jpeg';
  if (b.length >= 4 && b[0] == 0x89 && b[1] == 0x50 && b[2] == 0x4E && b[3] == 0x47) return 'image/png';
  return null;
}

typedef DateRange = ({DateTime from, DateTime to});

DateTime dateOnly(DateTime d) => DateTime(d.year, d.month, d.day);

bool crossesYear(DateTime from, DateTime to) => from.year != to.year;

/// Calendar days from..to inclusive (the server counts working days only).
int calendarSpan(DateTime from, DateTime to) =>
    dateOnly(to).difference(dateOnly(from)).inDays.abs() + 1;

/// Splits a range at year boundaries: 28 Dec 2026 - 3 Jan 2027 becomes
/// 28-31 Dec 2026 and 1-3 Jan 2027. Empty when `to < from`.
List<DateRange> splitByYear(DateTime from, DateTime to) {
  var s = dateOnly(from);
  final e = dateOnly(to);
  if (e.isBefore(s)) return const [];
  final out = <DateRange>[];
  while (s.year < e.year) {
    out.add((from: s, to: DateTime(s.year, 12, 31)));
    s = DateTime(s.year + 1);
  }
  out.add((from: s, to: e));
  return out;
}

String yearCrossingMessage(DateTime from, DateTime to) {
  final parts = splitByYear(from, to);
  final text = parts.map((r) => '${ymd(r.from)} to ${ymd(r.to)}').join(' and ');
  return 'Leave cannot span two calendar years. Submit it as separate requests: $text.';
}

/// Minutes of `HH:MM` text, or null.
int? parseHm(String? s) {
  if (s == null) return null;
  final m = RegExp(r'^(\d{1,2}):(\d{2})$').firstMatch(s);
  if (m == null) return null;
  final h = int.parse(m[1]!);
  final mi = int.parse(m[2]!);
  if (h > 23 || mi > 59) return null;
  return h * 60 + mi;
}

String formatHm(int hour, int minute) => '${two(hour)}:${two(minute)}';

/// Minutes between two `HH:MM` values; null when invalid.
int? minutesBetween(String? from, String? to) {
  final a = parseHm(from);
  final b = parseHm(to);
  if (a == null || b == null) return null;
  return b - a;
}

const int maxReasonLength = 500;

String? validateReason(String? v) {
  final s = v?.trim() ?? '';
  if (s.isEmpty) return 'Enter a reason';
  if (s.length > maxReasonLength) return 'Keep the reason under $maxReasonLength characters';
  return null;
}

/// First problem with a leave form, or null when it can be submitted.
String? validateLeaveForm({
  required LeaveType? type,
  required DateTime? from,
  required DateTime? to,
  required bool halfDay,
  required String? halfDaySession,
  required String reason,
}) {
  if (type == null) return 'Choose a leave type';
  if (from == null || to == null) return 'Choose the from and to dates';
  if (dateOnly(to).isBefore(dateOnly(from))) return 'The to date must be on or after the from date';
  if (halfDay) {
    if (!type.allowHalfDay) return '${type.name} does not allow half days';
    if (dateOnly(from) != dateOnly(to)) return 'A half day must be a single date';
    if (halfDaySession != 'first' && halfDaySession != 'second') return 'Choose the first or second half';
  }
  final max = type.maxConsecutiveDays;
  if (max != null && calendarSpan(from, to) > max) {
    return '${type.name} allows at most $max consecutive days';
  }
  return validateReason(reason);
}

/// First problem with a permission form, or null.
String? validatePermissionForm({
  required DateTime? date,
  required String? fromTime,
  required String? toTime,
  required String reason,
  required DateTime today,
}) {
  if (date == null) return 'Choose the date';
  if (dateOnly(date).isBefore(dateOnly(today))) return 'Permission cannot be requested for a past date';
  if (fromTime == null || toTime == null) return 'Choose the from and to times';
  final mins = minutesBetween(fromTime, toTime);
  if (mins == null || mins <= 0) return 'The to time must be after the from time';
  return validateReason(reason);
}

/// Apply: a missing leave type is a 404 meaning the type list is stale.
String applyErrorMessage(ApiException e) => e.code == 'NOT_FOUND'
    ? 'This leave type no longer exists. Pull to refresh the leave types and choose again.'
    : leaveErrorMessage(e);

/// User-facing text for leave / permission / approval domain errors.
String leaveErrorMessage(ApiException e) {
  switch (e.code) {
    case 'INSUFFICIENT_BALANCE':
      final left = double.tryParse(e.detail('remaining') ?? '');
      return left == null
          ? 'You do not have enough leave balance for this request.'
          : 'Not enough balance. You have ${daysLabel(left)} left for this leave type.';
    case 'OVERLAPPING_REQUEST':
      return 'This overlaps with another pending or approved request (or approved leave) of yours.';
    case 'PERMISSION_LIMIT_EXCEEDED':
      final f = e.details.isEmpty ? '' : e.details.first.field;
      if (f == 'count') return 'You have used all permission requests allowed this month.';
      if (f == 'minutes') return 'This would exceed your permission minutes for this month.';
      return 'You have reached this month\'s permission limit.';
    case 'NOT_PENDING':
      return 'This request is no longer pending.';
    case 'INVALID_DATE_RANGE':
      return '${e.message.isEmpty ? 'These dates are not allowed. Leave must be within one calendar year, '
          'not too far in the past and not more than $defaultMaxAdvanceDays days ahead.' : e.message} '
          '$mergedRequestsHint';
    case 'REQUESTER_INACTIVE':
      return 'This employee is no longer active, so the request cannot be approved. You can still reject it.';
    case 'TEMPORARILY_UNAVAILABLE':
      return 'The service is temporarily unavailable. Please try again in a moment.';
    case 'INVALID_TIME_RANGE':
      return e.message.isEmpty
          ? 'These times are not allowed. Use a future working day, times inside your shift, and a shorter duration.'
          : e.message;
    case 'ZERO_WORKING_DAYS':
      return 'The selected dates have no working days (weekly offs or holidays only).';
    case 'ATTACHMENT_REQUIRED':
      return 'This leave needs a supporting attachment. Add one and try again.';
    case 'HALF_DAY_NOT_ALLOWED':
      return 'This leave type does not allow half days.';
    case 'LEAVE_TYPE_INACTIVE':
      return 'This leave type is no longer available. Choose another.';
    case 'NO_SHIFT_ASSIGNED':
      return 'No shift is assigned to you yet. Please contact HR.';
    case 'FORBIDDEN':
      return 'You cannot approve or reject your own request.';
    case 'NOT_FOUND':
      return 'This request was not found. It may have been removed or is no longer yours.';
    case 'RATE_LIMITED':
      final w = e.retryAfter;
      return w == null
          ? 'Too many attempts. Wait a minute and try again.'
          : 'Too many attempts. Try again in ${waitText(w)}.';
    case 'VALIDATION_ERROR':
      return e.message;
    default:
      return e.message;
  }
}

// ---- push routing (api-contract section 11) ---------------------------------

enum RequestPushScreen { approvals, myRequests }

class RequestPushTarget {
  const RequestPushTarget(this.screen, {this.kind, this.id});
  final RequestPushScreen screen;
  final RequestKind? kind;
  final String? id;
}

/// Maps a push `data` payload (`type`, `id`) to a screen, or null if it is
/// not a leave/permission message. `new_request` goes to Approvals;
/// decisions go to My Requests.
RequestPushTarget? parseRequestPush(Map<String, dynamic> data) {
  final id = data['id'] as String?;
  return switch (data['type']) {
    'new_request' => const RequestPushTarget(RequestPushScreen.approvals),
    'leave_decision' => RequestPushTarget(RequestPushScreen.myRequests, kind: RequestKind.leave, id: id),
    'permission_decision' =>
      RequestPushTarget(RequestPushScreen.myRequests, kind: RequestKind.permission, id: id),
    _ => null,
  };
}

// ---- permissions ----------------------------------------------------------------

const approveAllPermission = 'request.approve_all';
const approveTeamPermission = 'request.approve_team';
const viewAllPermission = 'request.view_all';
const viewTeamPermission = 'request.view_team';

/// Whether the Approvals entry should be shown (server still enforces).
bool canOpenApprovals(List<String> permissions) => const [
      approveAllPermission,
      approveTeamPermission,
      viewAllPermission,
      viewTeamPermission,
    ].any(permissions.contains);

bool canDecideRequests(List<String> permissions) =>
    permissions.contains(approveAllPermission) || permissions.contains(approveTeamPermission);

/// `Everyone` or `Your team`.
String approvalScopeLabel(List<String> permissions) =>
    permissions.contains(approveAllPermission) || permissions.contains(viewAllPermission)
        ? 'All employees'
        : 'Your team';
