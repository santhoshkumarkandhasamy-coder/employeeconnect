import '../../../core/utils/dates.dart';

/// api-contract.md section 4 models.

enum RequestKind {
  leave('leave', 'Leave', 'leave'),
  permission('permission', 'Permission', 'permission'),
  onDuty('on_duty', 'On duty', 'on-duty');

  const RequestKind(this.wire, this.label, this.path);

  /// `type` in the admin endpoints and list items.
  final String wire;
  final String label;

  /// URL segment of the employee endpoints (`/<path>/requests`).
  final String path;

  /// Leave and on duty cover whole dates; permission is a time window.
  bool get isDateRange => this != permission;

  static RequestKind parse(Object? v) => switch (v) {
        'permission' => permission,
        'on_duty' => onDuty,
        _ => leave,
      };
}

/// An active client site for the on-duty form (`GET /on-duty/locations`).
/// Employees never get the coordinates here.
class ClientLocationOption {
  const ClientLocationOption({required this.id, required this.name, this.address});
  final String id;
  final String name;
  final String? address;

  factory ClientLocationOption.fromJson(Map<String, dynamic> j) => ClientLocationOption(
        id: '${j['id'] ?? ''}',
        name: '${j['name'] ?? ''}',
        address: j['address'] as String?,
      );

  static List<ClientLocationOption> parseList(Object json) {
    final raw = (json as Map<String, dynamic>)['items'];
    return [
      if (raw is List)
        for (final e in raw)
          if (e is Map<String, dynamic>) ClientLocationOption.fromJson(e),
    ];
  }
}

enum RequestStatus {
  pending('Pending'),
  approved('Approved'),
  rejected('Rejected'),
  cancelled('Cancelled'),
  unknown('Unknown');

  const RequestStatus(this.label);
  final String label;

  String get wire => name;

  static RequestStatus parse(Object? v) => switch (v) {
        'pending' => pending,
        'approved' => approved,
        'rejected' => rejected,
        'cancelled' => cancelled,
        _ => unknown,
      };
}

/// `10`, `0.5`, `2.5`.
String formatDays(num d) => d == d.roundToDouble() ? '${d.round()}' : d.toStringAsFixed(1);

String daysLabel(num d) => '${formatDays(d)} ${d == 1 ? 'day' : 'days'}';

class LeaveType {
  const LeaveType({
    required this.id,
    required this.code,
    required this.name,
    this.annualQuota = 0,
    this.isPaid = true,
    this.allowHalfDay = false,
    this.requiresAttachmentAfterDays,
    this.maxConsecutiveDays,
  });
  final String id;
  final String code;
  final String name;
  final double annualQuota;
  final bool isPaid;
  final bool allowHalfDay;
  final int? requiresAttachmentAfterDays;
  final int? maxConsecutiveDays;

  factory LeaveType.fromJson(Map<String, dynamic> j) => LeaveType(
        id: '${j['id'] ?? ''}',
        code: '${j['code'] ?? ''}',
        name: '${j['name'] ?? ''}',
        annualQuota: (j['annual_quota'] as num?)?.toDouble() ?? 0,
        isPaid: j['is_paid'] as bool? ?? true,
        allowHalfDay: j['allow_half_day'] as bool? ?? false,
        requiresAttachmentAfterDays: (j['requires_attachment_after_days'] as num?)?.toInt(),
        maxConsecutiveDays: (j['max_consecutive_days'] as num?)?.toInt(),
      );

  static List<LeaveType> parseList(Object json) {
    final raw = (json as Map<String, dynamic>)['items'];
    return [
      if (raw is List)
        for (final e in raw)
          if (e is Map<String, dynamic>) LeaveType.fromJson(e),
    ];
  }
}

class LeaveBalanceItem {
  const LeaveBalanceItem({
    required this.leaveTypeId,
    required this.code,
    required this.name,
    required this.quota,
    required this.used,
    required this.pending,
    required this.remaining,
  });
  final String leaveTypeId;
  final String code;
  final String name;
  final double quota;
  final double used;
  final double pending;
  final double remaining;

  factory LeaveBalanceItem.fromJson(Map<String, dynamic> j) {
    double n(String k) => (j[k] as num?)?.toDouble() ?? 0;
    return LeaveBalanceItem(
      leaveTypeId: '${j['leave_type_id'] ?? ''}',
      code: '${j['code'] ?? ''}',
      name: '${j['name'] ?? ''}',
      quota: n('quota'),
      used: n('used'),
      pending: n('pending'),
      remaining: n('remaining'),
    );
  }
}

class PermissionBalance {
  const PermissionBalance({
    this.month = '',
    this.limitCount = 0,
    this.usedCount = 0,
    this.pendingCount = 0,
    this.remainingCount = 0,
    this.limitMinutes = 0,
    this.usedMinutes = 0,
    this.pendingMinutes = 0,
    this.remainingMinutes = 0,
  });
  final String month;
  final int limitCount;
  final int usedCount;
  final int pendingCount;
  final int remainingCount;
  final int limitMinutes;
  final int usedMinutes;
  final int pendingMinutes;
  final int remainingMinutes;

  factory PermissionBalance.fromJson(Map<String, dynamic> j) {
    int n(String k) => (j[k] as num?)?.toInt() ?? 0;
    return PermissionBalance(
      month: '${j['month'] ?? ''}',
      limitCount: n('limit_count'),
      usedCount: n('used_count'),
      pendingCount: n('pending_count'),
      remainingCount: n('remaining_count'),
      limitMinutes: n('limit_minutes'),
      usedMinutes: n('used_minutes'),
      pendingMinutes: n('pending_minutes'),
      remainingMinutes: n('remaining_minutes'),
    );
  }
}

class BalanceData {
  const BalanceData({required this.year, required this.leave, required this.permission});
  final int year;
  final List<LeaveBalanceItem> leave;
  final PermissionBalance permission;

  LeaveBalanceItem? forType(String id) {
    for (final b in leave) {
      if (b.leaveTypeId == id) return b;
    }
    return null;
  }

  factory BalanceData.fromJson(Map<String, dynamic> j) {
    final raw = j['leave'];
    final perm = j['permission'];
    return BalanceData(
      year: (j['year'] as num?)?.toInt() ?? DateTime.now().year,
      leave: [
        if (raw is List)
          for (final e in raw)
            if (e is Map<String, dynamic>) LeaveBalanceItem.fromJson(e),
      ],
      permission: perm is Map<String, dynamic> ? PermissionBalance.fromJson(perm) : const PermissionBalance(),
    );
  }
}

class PersonSummary {
  const PersonSummary({
    required this.id,
    required this.employeeId,
    required this.fullName,
    this.department,
    this.photoUrl,
  });
  final String id;
  final String employeeId;
  final String fullName;
  final String? department;

  /// `/api/files/<id>` link to the profile photo (load with the authenticated
  /// file service, never as a plain URL).
  final String? photoUrl;

  String get display => fullName.isEmpty ? employeeId : fullName;

  static PersonSummary? parse(Object? v) => v is Map
      ? PersonSummary(
          id: '${v['id'] ?? ''}',
          employeeId: '${v['employee_id'] ?? ''}',
          fullName: '${v['full_name'] ?? ''}',
          department: v['department'] as String?,
          photoUrl: v['photo_url'] as String?,
        )
      : null;
}

/// A leave, permission or on-duty request (own request or admin list item).
class RequestItem {
  const RequestItem({
    required this.kind,
    required this.id,
    required this.status,
    this.reason = '',
    this.approver,
    this.user,
    this.decisionComment,
    this.decidedAt,
    this.createdAt,
    this.leaveTypeName,
    this.fromDate,
    this.toDate,
    this.halfDay = false,
    this.halfDaySession,
    this.days,
    this.attachmentUrl,
    this.attachmentContentType,
    this.date,
    this.fromTime,
    this.toTime,
    this.minutes,
    this.clientLocationName,
    this.clientLocationAddress,
  });

  final RequestKind kind;
  final String id;
  final RequestStatus status;
  final String reason;
  final PersonSummary? approver;
  final PersonSummary? user;
  final String? decisionComment;
  final DateTime? decidedAt;
  final DateTime? createdAt;

  // leave
  final String? leaveTypeName;
  final String? fromDate;
  final String? toDate;
  final bool halfDay;
  final String? halfDaySession;
  final double? days;
  /// `/api/files/<id>`; null when none or purged by retention.
  final String? attachmentUrl;

  /// `image/jpeg|image/png|application/pdf|null`.
  final String? attachmentContentType;

  // permission
  final String? date;
  final String? fromTime;
  final String? toTime;
  final int? minutes;

  // on duty
  final String? clientLocationName;
  final String? clientLocationAddress;

  bool get isPending => status == RequestStatus.pending;

  bool get hasAttachment => attachmentUrl != null && attachmentUrl!.isNotEmpty;

  String get title => switch (kind) {
        RequestKind.leave => leaveTypeName ?? 'Leave',
        RequestKind.permission => 'Permission',
        RequestKind.onDuty => clientLocationName == null ? 'On duty' : 'On duty: $clientLocationName',
      };

  /// Short date text, e.g. `Tue, 3 Nov 2026 to Thu, 5 Nov 2026` or `... 16:00-17:30`.
  String get whenText {
    String d(String? s) {
      final p = parseYmd(s);
      return p == null ? (s ?? '') : formatDayLong(p);
    }

    if (kind == RequestKind.permission) return '${d(date)}, $fromTime-$toTime';
    final base = fromDate == toDate ? d(fromDate) : '${d(fromDate)} to ${d(toDate)}';
    if (halfDay) return '$base (${halfDaySession == 'second' ? 'second' : 'first'} half)';
    return base;
  }

  /// `3 days` / `90 min`.
  String get amountText => kind.isDateRange ? daysLabel(days ?? 0) : formatDuration(minutes ?? 0);

  factory RequestItem.fromJson(Map<String, dynamic> j, {RequestKind? kind}) {
    final k = kind ?? RequestKind.parse(j['type']);
    final lt = j['leave_type'];
    final loc = j['client_location'];
    return RequestItem(
      kind: k,
      id: '${j['id'] ?? ''}',
      status: RequestStatus.parse(j['status']),
      reason: j['reason'] == null ? '' : '${j['reason']}',
      approver: PersonSummary.parse(j['approver']),
      user: PersonSummary.parse(j['user']),
      decisionComment: j['decision_comment'] as String?,
      decidedAt: parseTimestamp(j['decided_at']),
      createdAt: parseTimestamp(j['created_at']),
      leaveTypeName: lt is Map ? lt['name'] as String? : null,
      fromDate: j['from_date'] as String?,
      toDate: j['to_date'] as String?,
      halfDay: j['half_day'] as bool? ?? false,
      halfDaySession: j['half_day_session'] as String?,
      days: (j['days'] as num?)?.toDouble(),
      attachmentUrl: j['attachment_url'] as String?,
      attachmentContentType: j['attachment_content_type'] as String?,
      date: j['date'] as String?,
      fromTime: j['from_time'] as String?,
      toTime: j['to_time'] as String?,
      minutes: (j['minutes'] as num?)?.toInt(),
      clientLocationName: loc is Map ? loc['name'] as String? : null,
      clientLocationAddress: loc is Map ? loc['address'] as String? : null,
    );
  }
}
