import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import 'payroll_money.dart';

/// api-contract.md section 17.6 (reimbursement claims) and 17.8 (overtime),
/// work package 3b. In memory only, like the rest of payroll. Money stays a
/// string; minutes are integers.

String _s(Object? v) => v == null ? '' : '$v';
String? _opt(Object? v) => v is String && v.isNotEmpty ? v : null;
int _i(Object? v) => v is num ? v.toInt() : (int.tryParse('$v') ?? 0);
List<Map<String, dynamic>> _list(Object? v) => [
      if (v is List)
        for (final e in v)
          if (e is Map) e.cast<String, dynamic>(),
    ];
List<String> _strings(Object? v) => [
      if (v is List)
        for (final e in v) '$e',
    ];
EmployeeRef? _userOrNull(Object? v) => EmployeeRef.parse(v);

// ---- permissions --------------------------------------------------------------------

const String reimbursementSubmitPermission = 'reimbursement.submit';

bool canSubmitClaims(List<String> p) => p.contains(reimbursementSubmitPermission);

/// The manager step of claims (list + verify) needs one of the request
/// permissions; the server still scopes it to direct reports.
bool canSeeTeamClaims(List<String> p) =>
    const ['request.view_team', 'request.approve_team', 'request.approve_all'].any(p.contains);

// ---- bills ----------------------------------------------------------------------------

/// Largest bill the server accepts (`POST /files`, 10 MB).
const int claimBillMaxBytes = 10 * 1024 * 1024;

const List<String> claimBillExtensions = ['jpg', 'jpeg', 'png', 'pdf'];

/// Message for a bill that cannot be sent, or null. Checked on the picked
/// file's name and reported size BEFORE any byte is read.
String? validateClaimBill({required String name, required int size}) {
  final dot = name.lastIndexOf('.');
  final ext = dot < 0 ? '' : name.substring(dot + 1).toLowerCase();
  if (!claimBillExtensions.contains(ext)) return 'Use a JPEG, PNG or PDF file.';
  if (size <= 0) return 'The file is empty.';
  if (size > claimBillMaxBytes) return 'The file is too large (10 MB at most).';
  return null;
}

String billContentTypeFor(String name) {
  final n = name.toLowerCase();
  if (n.endsWith('.pdf')) return 'application/pdf';
  if (n.endsWith('.png')) return 'image/png';
  return 'image/jpeg';
}

class ClaimBill {
  const ClaimBill({required this.url, required this.contentType});
  final String url;
  final String contentType;

  bool get isPdf => contentType == 'application/pdf';
}

// ---- categories -------------------------------------------------------------------------

class ClaimCategory {
  const ClaimCategory({
    required this.code,
    required this.name,
    this.maxAmount,
    this.billRequired = true,
  });

  factory ClaimCategory.fromJson(Map<String, dynamic> j) => ClaimCategory(
        code: _s(j['code']),
        name: _s(j['name']),
        maxAmount: _opt(j['max_amount']),
        billRequired: j['bill_required'] != false,
      );

  final String code;
  final String name;

  /// Money string, null = no cap.
  final String? maxAmount;
  final bool billRequired;
}

class ClaimCategories {
  const ClaimCategories({required this.items, required this.maxAgeDays});

  factory ClaimCategories.fromJson(Map<String, dynamic> j) => ClaimCategories(
        items: [for (final c in _list(j['items'])) ClaimCategory.fromJson(c)],
        maxAgeDays: _i(j['max_age_days']) == 0 ? 60 : _i(j['max_age_days']),
      );

  final List<ClaimCategory> items;
  final int maxAgeDays;

  ClaimCategory? byCode(String? code) {
    for (final c in items) {
      if (c.code == code) return c;
    }
    return null;
  }
}

class ClaimCategoryRef {
  const ClaimCategoryRef({required this.code, required this.name});
  final String code;
  final String name;

  String get label => name.isEmpty ? code : name;
}

// ---- claims -----------------------------------------------------------------------------

abstract final class ClaimStatus {
  static const submitted = 'submitted';
  static const verified = 'verified';
  static const approved = 'approved';
  static const processing = 'processing'; // admin API only
  static const paid = 'paid';
  static const rejected = 'rejected';
  static const cancelled = 'cancelled';
}

const Map<String, String> claimStatusLabels = {
  'submitted': 'Submitted',
  'verified': 'Verified',
  'approved': 'Approved',
  'processing': 'In payslip',
  'paid': 'Paid',
  'rejected': 'Rejected',
  'cancelled': 'Cancelled',
};

String claimStatusLabel(String s) => claimStatusLabels[s] ?? s;

/// Statuses the employee can filter on (the employee API folds `processing`
/// into `approved`).
const List<String> claimFilterStatuses = [
  'submitted',
  'verified',
  'approved',
  'paid',
  'rejected',
  'cancelled',
];

const Map<String, String> claimFlagLabels = {
  'PERIOD_CLOSED': 'Month already published: will not be paid until moved',
};

String claimFlagLabel(String f) => claimFlagLabels[f] ?? f;

class Claim {
  const Claim({
    required this.id,
    required this.category,
    required this.expenseDate,
    required this.amount,
    required this.description,
    required this.status,
    required this.version,
    this.approvedAmount,
    this.bill,
    this.managerStep = 'pending',
    this.period,
    this.verifier,
    this.verifiedAt,
    this.approver,
    this.decidedAt,
    this.decisionComment,
    this.createdAt,
    this.user,
    this.payslipId,
    this.runStatus,
    this.flags = const [],
  });

  factory Claim.fromJson(Map<String, dynamic> j) {
    final cat = j['category'];
    final bill = j['bill'];
    return Claim(
      id: _s(j['id']),
      category: cat is Map
          ? ClaimCategoryRef(code: _s(cat['code']), name: _s(cat['name']))
          : ClaimCategoryRef(code: _s(cat), name: ''),
      expenseDate: _s(j['expense_date']),
      amount: _s(j['amount']),
      approvedAmount: _opt(j['approved_amount']),
      description: _s(j['description']),
      bill: bill is Map && _opt(bill['url']) != null
          ? ClaimBill(url: _s(bill['url']), contentType: _s(bill['content_type']))
          : null,
      status: _opt(j['status']) ?? ClaimStatus.submitted,
      managerStep: _opt(j['manager_step']) ?? 'pending',
      period: _opt(j['period']),
      verifier: _userOrNull(j['verifier']),
      verifiedAt: parseTimestamp(j['verified_at']),
      approver: _userOrNull(j['approver']),
      decidedAt: parseTimestamp(j['decided_at']),
      decisionComment: _opt(j['decision_comment']),
      createdAt: parseTimestamp(j['created_at']),
      version: _i(j['version']),
      user: _userOrNull(j['user']),
      payslipId: _opt(j['payslip_id']),
      runStatus: _opt(j['run_status']),
      flags: _strings(j['flags']),
    );
  }

  final String id;
  final ClaimCategoryRef category;
  final String expenseDate;
  final String amount;
  final String? approvedAmount;
  final String description;
  final ClaimBill? bill;
  final String status;
  final String managerStep;
  final String? period;
  final EmployeeRef? verifier;
  final DateTime? verifiedAt;
  final EmployeeRef? approver;
  final DateTime? decidedAt;
  final String? decisionComment;
  final DateTime? createdAt;
  final int version;

  /// Admin shape only.
  final EmployeeRef? user;
  final String? payslipId;
  final String? runStatus;
  final List<String> flags;

  /// The employee may cancel while submitted or verified.
  bool get canCancel => status == ClaimStatus.submitted || status == ClaimStatus.verified;

  /// Waiting for the manager (verify / reject).
  bool get awaitingManager => status == ClaimStatus.submitted && managerStep == 'pending';

  /// In the HR queue: verified, or submitted with the manager step skipped.
  bool get awaitingHr =>
      status == ClaimStatus.verified || (status == ClaimStatus.submitted && managerStep == 'skipped');

  bool get isApproved => status == ClaimStatus.approved;

  /// Where the money lands, e.g. `November 2026`.
  String? get periodText => period == null ? null : monthTitle(period!);
}

/// Own claims: optional status and year filter.
class ClaimQuery {
  const ClaimQuery({this.status, this.year});
  final String? status;
  final int? year;

  @override
  bool operator ==(Object other) => other is ClaimQuery && other.status == status && other.year == year;

  @override
  int get hashCode => Object.hash(status, year);
}

/// `stage` is `manager`, `hr` or `all`.
class AdminClaimQuery {
  const AdminClaimQuery({required this.stage, this.status, this.period, this.category, this.employeeId});
  final String stage;
  final String? status;
  final String? period;
  final String? category;
  final String? employeeId;

  @override
  bool operator ==(Object other) =>
      other is AdminClaimQuery &&
      other.stage == stage &&
      other.status == status &&
      other.period == period &&
      other.category == category &&
      other.employeeId == employeeId;

  @override
  int get hashCode => Object.hash(stage, status, period, category, employeeId);
}

// ---- claim form validation -----------------------------------------------------------------

/// Today in the office timezone is approximated by the device date; the server
/// stays the authority (`INVALID_DATE`).
String? validateExpenseDate(DateTime? date, DateTime today, int maxAgeDays) {
  if (date == null) return 'Choose the expense date';
  final d = DateTime(date.year, date.month, date.day);
  final t = DateTime(today.year, today.month, today.day);
  if (d.isAfter(t)) return 'The expense date cannot be in the future';
  if (t.difference(d).inDays > maxAgeDays) {
    return 'Claims can be made for expenses up to $maxAgeDays days old';
  }
  return null;
}

String? validateClaimAmount(String? v, ClaimCategory? category) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return 'Enter the amount';
  final paise = parseMoneyPaise(t);
  if (paise == null) return 'Enter an amount like 1240 or 1240.50';
  if (paise <= 0) return 'The amount must be above zero';
  final cap = category?.maxAmount;
  if (cap != null && paise > moneyToPaise(cap)) {
    return 'The limit for ${category!.name} is ${formatRupees(cap)}';
  }
  return null;
}

String? validateClaimDescription(String? v) {
  final t = (v ?? '').trim();
  if (t.length < 3) return 'Describe the expense (3 to 300 characters)';
  if (t.length > 300) return 'At most 300 characters';
  return null;
}

/// Approved amount: above zero and not above the amount claimed.
String? validateApprovedAmount(String? v, String claimed) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return 'Enter the amount to approve';
  final paise = parseMoneyPaise(t);
  if (paise == null) return 'Enter an amount like 1240 or 1240.50';
  if (paise <= 0) return 'The amount must be above zero';
  if (paise > moneyToPaise(claimed)) return 'Cannot be above the amount claimed (${formatRupees(claimed)})';
  return null;
}

/// Required comment (reject) 3..200 characters, or optional.
String? validateClaimComment(String? v, {required bool required}) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return required ? 'A comment is required' : null;
  if (required && t.length < 3) return 'At least 3 characters';
  if (t.length > 200) return 'At most 200 characters';
  return null;
}

/// Text for the form-level error of a failed submit. `AMOUNT_ABOVE_LIMIT`
/// carries the category cap in `details[0].message`.
String? claimCapFromError(ApiException e) {
  if (e.code != 'AMOUNT_ABOVE_LIMIT') return null;
  for (final d in e.details) {
    if ((d.field == 'amount' || d.field.isEmpty) && parseMoneyPaise(d.message) != null) return d.message;
  }
  return null;
}

// ---- overtime -----------------------------------------------------------------------------------

abstract final class OvertimeStatus {
  static const computed = 'computed';
  static const approved = 'approved';
  static const rejected = 'rejected';
}

const Map<String, String> overtimeStatusLabels = {
  'computed': 'To review',
  'approved': 'Approved',
  'rejected': 'Rejected',
  'processing': 'In payslip',
  'paid': 'Paid',
};

String overtimeStatusLabel(String s) => overtimeStatusLabels[s] ?? s;

const Map<String, String> overtimeFlagLabels = {
  'OFF_DAY': 'Holiday or weekly off',
  'AUTO_CLOSED': 'Auto-closed day',
  'SUSPICIOUS': 'Suspicious attendance',
  'ATTENDANCE_CHANGED': 'Attendance changed after review',
};

String overtimeFlagLabel(String f) => overtimeFlagLabels[f] ?? f;

/// `7h 30m`; zero is `0m`.
String minutesText(int minutes) => formatDuration(minutes);

class OvertimeDay {
  const OvertimeDay({
    required this.id,
    required this.user,
    required this.workDate,
    required this.computedMinutes,
    required this.status,
    required this.version,
    this.approvedMinutes,
    this.flags = const [],
    this.reviewer,
    this.reviewedAt,
    this.comment,
  });

  factory OvertimeDay.fromJson(Map<String, dynamic> j) => OvertimeDay(
        id: _s(j['id']),
        user: _userOrNull(j['user']) ?? const EmployeeRef(id: '', employeeId: '', fullName: ''),
        workDate: _s(j['work_date']),
        computedMinutes: _i(j['computed_minutes']),
        approvedMinutes: j['approved_minutes'] is num ? (j['approved_minutes'] as num).toInt() : null,
        flags: _strings(j['flags']),
        status: _opt(j['status']) ?? OvertimeStatus.computed,
        reviewer: _userOrNull(j['reviewer']),
        reviewedAt: parseTimestamp(j['reviewed_at']),
        comment: _opt(j['comment']),
        version: _i(j['version']),
      );

  final String id;
  final EmployeeRef user;
  final String workDate;
  final int computedMinutes;
  final int? approvedMinutes;
  final List<String> flags;
  final String status;
  final EmployeeRef? reviewer;
  final DateTime? reviewedAt;
  final String? comment;
  final int version;

  /// computed / approved / rejected rows can be reviewed; processing / paid cannot.
  bool get reviewable =>
      status == OvertimeStatus.computed || status == OvertimeStatus.approved || status == OvertimeStatus.rejected;
}

class OvertimeSummary {
  const OvertimeSummary({
    required this.userId,
    required this.approvedMinutes,
    required this.computedMinutes,
    required this.pendingRows,
  });

  factory OvertimeSummary.fromJson(Map<String, dynamic> j) => OvertimeSummary(
        userId: _s(j['user_id']),
        approvedMinutes: _i(j['approved_minutes']),
        computedMinutes: _i(j['computed_minutes']),
        pendingRows: _i(j['pending_rows']),
      );

  static List<OvertimeSummary> parseList(Map<String, dynamic> j) =>
      [for (final e in _list(j['summary'])) OvertimeSummary.fromJson(e)];

  final String userId;
  final int approvedMinutes;
  final int computedMinutes;
  final int pendingRows;
}

class OvertimeQuery {
  const OvertimeQuery({required this.period, this.status, this.userId});
  final String period;
  final String? status;
  final String? userId;

  @override
  bool operator ==(Object other) =>
      other is OvertimeQuery && other.period == period && other.status == status && other.userId == userId;

  @override
  int get hashCode => Object.hash(period, status, userId);
}

class OvertimeComputeResult {
  const OvertimeComputeResult({
    required this.period,
    required this.created,
    required this.updated,
    required this.flaggedChanged,
  });

  factory OvertimeComputeResult.fromJson(Map<String, dynamic> j) => OvertimeComputeResult(
        period: _s(j['period']),
        created: _i(j['created']),
        updated: _i(j['updated']),
        flaggedChanged: _i(j['flagged_changed']),
      );

  final String period;
  final int created;
  final int updated;
  final int flaggedChanged;
}

class OvertimeBulkSkip {
  const OvertimeBulkSkip({required this.id, required this.reason});
  final String id;
  final String reason;
}

const Map<String, String> overtimeSkipReasons = {
  'ITEM_LOCKED': 'already in a payslip',
  'FORBIDDEN': 'your own overtime',
  'NOT_FOUND': 'not found',
  'PERIOD_LOCKED': 'month is locked',
  'NEEDS_REVIEW': 'needs a single review (flagged)',
};

class OvertimeBulkResult {
  const OvertimeBulkResult({required this.updated, required this.skipped});

  factory OvertimeBulkResult.fromJson(Map<String, dynamic> j) => OvertimeBulkResult(
        updated: _i(j['updated']),
        skipped: [
          for (final s in _list(j['skipped'])) OvertimeBulkSkip(id: _s(s['id']), reason: _s(s['reason'])),
        ],
      );

  final int updated;
  final List<OvertimeBulkSkip> skipped;

  /// "40 rows updated. 2 skipped: 1 already in a payslip, 1 month is locked."
  String get message {
    final b = StringBuffer('$updated ${updated == 1 ? 'row' : 'rows'} updated.');
    if (skipped.isNotEmpty) {
      final counts = <String, int>{};
      for (final s in skipped) {
        counts[s.reason] = (counts[s.reason] ?? 0) + 1;
      }
      final parts = [for (final e in counts.entries) '${e.value} ${overtimeSkipReasons[e.key] ?? e.key}'];
      b.write(' ${skipped.length} skipped: ${parts.join(', ')}.');
    }
    return b.toString();
  }
}

/// Most ids a bulk call accepts.
const int overtimeBulkMax = 200;

/// Minutes to approve: a whole number from 0 to [computed].
String? validateApprovedMinutes(String? v, int computed) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return 'Enter the minutes';
  final n = int.tryParse(t);
  if (n == null) return 'Enter whole minutes';
  if (n < 0) return 'Cannot be negative';
  if (n > computed) return 'Cannot be above the computed ${minutesText(computed)} ($computed min)';
  return null;
}

/// `GET /me/overtime`. `enabled == false` means "show nothing".
class MyOvertimeDay {
  const MyOvertimeDay({
    required this.workDate,
    required this.computedMinutes,
    required this.status,
    this.approvedMinutes,
  });

  factory MyOvertimeDay.fromJson(Map<String, dynamic> j) => MyOvertimeDay(
        workDate: _s(j['work_date']),
        computedMinutes: _i(j['computed_minutes']),
        approvedMinutes: j['approved_minutes'] is num ? (j['approved_minutes'] as num).toInt() : null,
        status: _opt(j['status']) ?? OvertimeStatus.computed,
      );

  final String workDate;
  final int computedMinutes;
  final int? approvedMinutes;
  final String status;
}

class MyOvertime {
  const MyOvertime({required this.enabled, this.eligible = false, this.period, this.items = const []});

  factory MyOvertime.fromJson(Map<String, dynamic> j) => MyOvertime(
        enabled: j['enabled'] == true,
        eligible: j['eligible'] == true,
        period: _opt(j['period']),
        items: [for (final e in _list(j['items'])) MyOvertimeDay.fromJson(e)],
      );

  final bool enabled;
  final bool eligible;
  final String? period;
  final List<MyOvertimeDay> items;

  /// Whether the employee should see overtime at all.
  bool get visible => enabled && eligible;

  int get totalApprovedMinutes => items.fold(0, (a, e) => a + (e.approvedMinutes ?? 0));
}

// ---- settings: reimbursement categories ----------------------------------------------------------

final RegExp _categoryCodeRe = RegExp(r'^[a-z][a-z0-9_]{1,19}$');

class CategoryDraft {
  const CategoryDraft({required this.code, required this.name, this.maxAmount, this.billRequired = true});

  static List<CategoryDraft> parseList(Object? v) => [
        if (v is List)
          for (final e in v)
            if (e is Map)
              CategoryDraft(
                code: _s(e['code']),
                name: _s(e['name']),
                maxAmount: _opt(e['max_amount']),
                billRequired: e['bill_required'] != false,
              ),
      ];

  final String code;
  final String name;
  final String? maxAmount;
  final bool billRequired;

  Map<String, Object?> toJson() => {
        'code': code.trim(),
        'name': name.trim(),
        'max_amount': maxAmount == null || maxAmount!.trim().isEmpty
            ? null
            : (normalizeMoney(maxAmount) ?? maxAmount!.trim()),
        'bill_required': billRequired,
      };
}

/// Client-side check of `payroll.reimbursement_categories`; the server stays
/// the authority. Returns a message or null.
String? validateCategories(List<CategoryDraft> list) {
  if (list.length > 20) return 'At most 20 categories';
  final seen = <String>{};
  for (var i = 0; i < list.length; i++) {
    final c = list[i];
    final row = 'Category ${i + 1}';
    if (!_categoryCodeRe.hasMatch(c.code.trim())) {
      return '$row: the code is 2 to 20 characters, starts with a letter, then a-z, 0-9 or _';
    }
    if (!seen.add(c.code.trim())) return '$row: the code ${c.code.trim()} is used twice';
    final n = c.name.trim();
    if (n.isEmpty || n.length > 40) return '$row: the name is 1 to 40 characters';
    final m = c.maxAmount;
    if (m != null && m.trim().isNotEmpty) {
      final p = parseMoneyPaise(m);
      if (p == null || p <= 0) return '$row: the limit must be an amount above zero, or empty for none';
    }
  }
  return null;
}
