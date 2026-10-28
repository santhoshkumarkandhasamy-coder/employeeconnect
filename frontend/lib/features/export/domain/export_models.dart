import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';

/// api-contract.md section 10 (`GET /admin/export`).
enum ExportType {
  all('all', 'All reports', 'Attendance, Leave, Late, Permission and On Duty in one workbook'),
  attendance('attendance', 'Attendance', 'Check-in and check-out times and status'),
  leave('leave', 'Leave', 'Leave requests overlapping the dates'),
  late('late', 'Late', 'Late arrivals'),
  permission('permission', 'Permission', 'Short permission requests'),
  onDuty('on_duty', 'On Duty', 'On-duty requests at client locations'),
  food('food', 'Food', 'Who needs food on each date');

  const ExportType(this.apiValue, this.label, this.description);
  final String apiValue;
  final String label;
  final String description;
}

/// Contract: max range 366 days, dates within 2000-01-01..2200-12-31.
const int exportMaxDays = 366;

/// Permissions of which the user needs at least one, per sheet
/// (contract, review 04: per-type permission on top of `export.all|team`).
const Map<ExportType, List<String>> _sheetPerms = {
  ExportType.attendance: ['attendance.view_all', 'attendance.view_team'],
  ExportType.leave: ['request.view_all', 'request.view_team'],
  ExportType.permission: ['request.view_all', 'request.view_team'],
  ExportType.onDuty: ['request.view_all', 'request.view_team'],
  ExportType.late: ['late.view_all', 'late.view_team'],
  ExportType.food: ['food.view_all'],
};

bool _hasExport(List<String> p) => p.contains('export.all') || p.contains('export.team');

bool canExportType(List<String> permissions, ExportType type) {
  if (!_hasExport(permissions)) return false;
  if (type == ExportType.all) {
    return [
      ExportType.attendance,
      ExportType.leave,
      ExportType.late,
      ExportType.permission,
      ExportType.onDuty,
    ].every((t) => canExportType(permissions, t));
  }
  return (_sheetPerms[type] ?? const <String>[]).any(permissions.contains);
}

/// Report types the user may request (server still enforces).
List<ExportType> allowedExportTypes(List<String> permissions) =>
    [for (final t in ExportType.values) if (canExportType(permissions, t)) t];

/// Whether the Export entry appears in More.
bool canOpenExport(List<String> permissions) => allowedExportTypes(permissions).isNotEmpty;

/// Inclusive number of days in [from]..[to].
int rangeDays(DateTime from, DateTime to) {
  final a = DateTime.utc(from.year, from.month, from.day);
  final b = DateTime.utc(to.year, to.month, to.day);
  return b.difference(a).inDays + 1;
}

/// Null when valid, else a user message.
String? validateExportRange(DateTime? from, DateTime? to) {
  if (from == null || to == null) return 'Choose a date range.';
  if (to.isBefore(from)) return 'The end date must not be before the start date.';
  if (from.year < 2000 || to.year > 2200) return 'Choose dates between 2000 and 2200.';
  if (rangeDays(from, to) > exportMaxDays) {
    return 'The range can be at most $exportMaxDays days.';
  }
  return null;
}

/// Optional business employee id (case-insensitive on the server).
String? validateEmployeeIdFilter(String? v) {
  final s = (v ?? '').trim();
  if (s.isEmpty) return null;
  if (!RegExp(r'^[A-Za-z0-9._-]{1,32}$').hasMatch(s)) {
    return 'Use letters, numbers, dot, dash or underscore only.';
  }
  return null;
}

typedef ExportPreset = ({String label, DateTime from, DateTime to});

List<ExportPreset> exportPresets(DateTime today) {
  final t = DateTime(today.year, today.month, today.day);
  return [
    (label: 'This month', from: DateTime(t.year, t.month), to: t),
    (
      label: 'Last month',
      from: DateTime(t.year, t.month - 1),
      to: DateTime(t.year, t.month, 0),
    ),
    (label: 'Last 30 days', from: t.subtract(const Duration(days: 29)), to: t),
  ];
}

String exportRangeLabel(DateTime from, DateTime to) =>
    from == to ? formatDayLong(from) : '${formatDayLong(from)} to ${formatDayLong(to)}';

/// `include_reasons=true` needs `export.sensitive` (contract section 10); the
/// server refuses it with 403 without the permission.
bool canExportReasons(List<String> permissions) => permissions.contains('export.sensitive');

/// Whether [type] has sheets with reason columns (Leave, Permission, On Duty).
/// The flag has no effect for attendance, late and food.
bool exportTypeHasReasons(ExportType type) =>
    type == ExportType.all ||
    type == ExportType.leave ||
    type == ExportType.permission ||
    type == ExportType.onDuty;

/// True when the user can only export their direct reports (`export.team`
/// without `export.all`): the server then also limits how far back it goes.
bool isTeamScopedExport(List<String> permissions) =>
    permissions.contains('export.team') && !permissions.contains('export.all');

/// "about 3 hours", "about 12 minutes", "30 seconds" for a Retry-After value.
String waitLabel(Duration d) {
  if (d.inSeconds < 90) return '${d.inSeconds < 1 ? 1 : d.inSeconds} seconds';
  if (d.inMinutes < 90) return 'about ${d.inMinutes} minutes';
  return 'about ${(d.inMinutes / 60).round()} hours';
}

/// Friendly message for export failures. Never includes server data.
/// [includeReasons] tells whether the failed request asked for reasons, so a
/// 403 can be explained (the server never silently downgrades the request).
String exportErrorMessage(ApiException e, {bool includeReasons = false}) {
  final wait = e.retryAfter;
  switch (e.code) {
    case 'EXPORT_QUOTA_EXCEEDED':
      return wait == null
          ? 'You have reached the daily export limit. Please try again tomorrow.'
          : 'You have reached the daily export limit. You can export again in ${waitLabel(wait)}.';
    case 'RANGE_TOO_LARGE':
      return 'That is too much data for one file. Choose a shorter date range.';
    case 'INVALID_DATE':
    case 'INVALID_DATE_RANGE':
      return 'Please check the dates. The end date must not be before the start date.';
    case 'VALIDATION_ERROR':
      return 'This report could not be created with these options. It may not be available yet.';
    case 'FILE_TOO_LARGE':
      return 'That report is too large to download. Choose a shorter date range.';
    case 'FORBIDDEN':
      // Team-scoped users get 403 (not 404) for an unknown id and for one
      // outside their team, so both cases share one message.
      return includeReasons
          ? 'You do not have access to this report, to the reasons in it, or to the selected '
              'department or employee. Turn off "Include reasons" or change the filters.'
          : 'You do not have access to this report, or the selected department or employee '
              'is not part of your team (or does not exist).';
    case 'NOT_FOUND':
      return 'That department or employee was not found.';
    case 'RATE_LIMITED':
      return wait == null
          ? 'Too many exports. Please wait a minute and try again.'
          : 'Too many exports. Please try again in ${waitLabel(wait)}.';
    case 'TEMPORARILY_UNAVAILABLE':
      return wait == null
          ? 'The server is busy creating other exports. Please try again shortly.'
          : 'The server is busy creating other exports. Please try again in ${waitLabel(wait)}.';
    case ApiException.networkCode:
      return e.message;
    default:
      if (e.status == 429) return 'Too many exports. Please wait a minute and try again.';
      return 'Could not create the report. Please try again.';
  }
}
