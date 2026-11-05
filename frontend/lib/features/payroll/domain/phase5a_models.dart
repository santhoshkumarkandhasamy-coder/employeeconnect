import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import '../../attendance/domain/attendance_models.dart' show Paged;
import 'payroll_models.dart' show PayrollRun, Payslip, PublishRequestRef, payrollErrorMessage, periodTitle;

/// api-contract.md section 19 (payroll phase 5, work package 5a): publish
/// requests, payslip delivery, company bank accounts and payment batches.
/// In memory only. Money, bank accounts, IFSC, references and beneficiary data
/// stay strings exactly as the server sends them (masked) and are never logged:
/// no toString overrides, no print, nothing here is persisted or put in an
/// exception.

String _s(Object? v) => v == null ? '' : '$v';
String? _opt(Object? v) => v is String && v.isNotEmpty ? v : null;
int _i(Object? v) => v is num ? v.toInt() : (int.tryParse('$v') ?? 0);
Map<String, dynamic> _m(Object? v) => v is Map ? v.cast<String, dynamic>() : const <String, dynamic>{};
List<Map<String, dynamic>> _list(Object? v) => [
      if (v is List)
        for (final e in v)
          if (e is Map) e.cast<String, dynamic>(),
    ];
List<String> _strings(Object? v) => [
      if (v is List)
        for (final e in v) '$e',
    ];

EmployeeRef _user(Object? v) =>
    EmployeeRef.parse(v) ?? const EmployeeRef(id: '', employeeId: '', fullName: '');

// ---- settings keys ------------------------------------------------------------------------

const String bankFileSecondPersonKey = 'payroll.bank_file_requires_second_person';

// ---- publish requests (19.2) --------------------------------------------------------------

const Map<String, String> publishStatusLabels = {
  'submitted': 'Waiting for approval',
  'approved': 'Approved and published',
  'rejected': 'Rejected',
  'withdrawn': 'Withdrawn',
  'stale': 'Out of date',
};

String publishStatusLabel(String s) => publishStatusLabels[s] ?? s;

const Map<String, String> publishStaleLabels = {
  'RUN_REOPENED': 'The run was reopened after the request.',
  'RUN_REFRESHED': 'The run was refreshed after the request.',
  'PAYSLIP_CHANGED': 'A payslip changed after the request.',
  'EXPIRED': 'The request expired before anybody decided.',
};

String publishStaleLabel(String? reason) =>
    reason == null ? 'The payroll changed after the request.' : (publishStaleLabels[reason] ?? 'The payroll changed after the request.');

const Map<String, String> publishReasonLabels = {
  'PROCESSED_RUN': 'processed the run',
  'EDITED_PAYSLIP': 'edited a payslip in it',
  'CREATED_COMPONENT': 'created a pay item that is in it',
  'CREATED_STRUCTURE': 'created a salary structure used in it',
  'FNF_MAKER': 'prepared a final settlement in it',
  'REQUESTED': 'asked for this publish',
  'OWN_PAYSLIP': 'this is your own payslip',
};

String publishReasonLabel(String r) => publishReasonLabels[r] ?? r;

/// Request scope: a whole run or one payslip of a published run.
bool isPayslipScope(String scope) => scope == 'payslip';

class PublishTotals {
  const PublishTotals({
    required this.grossEarnings,
    required this.totalDeductions,
    required this.netPay,
    required this.employerCost,
  });
  final String grossEarnings;
  final String totalDeductions;
  final String netPay;
  final String employerCost;

  static PublishTotals? parse(Object? v) {
    if (v is! Map) return null;
    final j = v.cast<String, dynamic>();
    return PublishTotals(
      grossEarnings: _s(j['gross_earnings']),
      totalDeductions: _s(j['total_deductions']),
      netPay: _s(j['net_pay']),
      employerCost: _s(j['employer_cost']),
    );
  }
}

class PublishSnapshot {
  const PublishSnapshot({this.payslips = 0, this.held = 0, this.warnings = 0, this.processedAt, this.totals});
  final int payslips;
  final int held;
  final int warnings;
  final DateTime? processedAt;

  /// Absent on list items.
  final PublishTotals? totals;

  factory PublishSnapshot.fromJson(Map<String, dynamic> j) => PublishSnapshot(
        payslips: _i(j['payslips']),
        held: _i(j['held']),
        warnings: _i(j['warnings']),
        processedAt: parseTimestamp(j['processed_at']),
        totals: PublishTotals.parse(j['totals']),
      );
}

class PublishRequest {
  const PublishRequest({
    required this.id,
    required this.scope,
    required this.runId,
    required this.runPeriod,
    required this.status,
    required this.requestedBy,
    required this.version,
    this.runStatus = '',
    this.payslipId,
    this.payslipUser,
    this.payslipRevision,
    this.staleReason,
    this.snapshot = const PublishSnapshot(),
    this.comment,
    this.requestedAt,
    this.expiresAt,
    this.makers,
    this.decidedBy,
    this.decidedAt,
    this.decisionComment,
  });

  final String id;
  final String scope;
  final String runId;
  final String runPeriod;
  final String runStatus;
  final String? payslipId;
  final EmployeeRef? payslipUser;
  final int? payslipRevision;
  final String status;
  final String? staleReason;
  final PublishSnapshot snapshot;
  final String? comment;
  final EmployeeRef requestedBy;
  final DateTime? requestedAt;
  final DateTime? expiresAt;

  /// Filled for approved and rejected requests only.
  final List<EmployeeRef>? makers;
  final EmployeeRef? decidedBy;
  final DateTime? decidedAt;
  final String? decisionComment;
  final int version;

  bool get isSubmitted => status == 'submitted';
  bool get isPayslip => isPayslipScope(scope);

  /// Run id for a run request, payslip id for a payslip request: the id the
  /// publish-check endpoint takes.
  String get checkId => isPayslip ? (payslipId ?? '') : runId;

  factory PublishRequest.fromJson(Map<String, dynamic> j) {
    final run = _m(j['run']);
    final slip = j['payslip'] is Map ? _m(j['payslip']) : null;
    final makers = j['makers'];
    return PublishRequest(
      id: _s(j['id']),
      scope: _opt(j['scope']) ?? 'run',
      runId: _s(run['id']),
      runPeriod: _s(run['period']),
      runStatus: _s(run['status']),
      payslipId: slip == null ? null : _s(slip['id']),
      payslipUser: slip == null ? null : EmployeeRef.parse(slip['user']),
      payslipRevision: slip == null ? null : _i(slip['revision']),
      status: _opt(j['status']) ?? 'submitted',
      staleReason: _opt(j['stale_reason']),
      snapshot: PublishSnapshot.fromJson(_m(j['snapshot'])),
      comment: _opt(j['comment']),
      requestedBy: _user(j['requested_by']),
      requestedAt: parseTimestamp(j['requested_at']),
      expiresAt: parseTimestamp(j['expires_at']),
      makers: makers is List ? [for (final e in makers) _user(e)] : null,
      decidedBy: EmployeeRef.parse(j['decided_by']),
      decidedAt: parseTimestamp(j['decided_at']),
      decisionComment: _opt(j['decision_comment']),
      version: _i(j['version']) == 0 ? 1 : _i(j['version']),
    );
  }
}

class PublishRequestQuery {
  const PublishRequestQuery({this.status, this.runId});
  final String? status;
  final String? runId;

  @override
  bool operator ==(Object other) =>
      other is PublishRequestQuery && other.status == status && other.runId == runId;

  @override
  int get hashCode => Object.hash(status, runId);
}

/// Result of approving: the request plus the run (scope run) or payslip (scope payslip).
class PublishApproveResult {
  const PublishApproveResult({required this.request, this.run, this.payslip});
  final PublishRequest request;
  final PayrollRun? run;
  final Payslip? payslip;

  factory PublishApproveResult.fromJson(Map<String, dynamic> j) => PublishApproveResult(
        request: PublishRequest.fromJson(_m(j['request'])),
        run: j['run'] is Map ? PayrollRun.fromJson(_m(j['run'])) : null,
        payslip: j['payslip'] is Map ? Payslip.fromJson(_m(j['payslip'])) : null,
      );
}

class PublishMaker {
  const PublishMaker({required this.user, required this.reasons});
  final EmployeeRef user;
  final List<String> reasons;
}

/// `GET .../publish-check` (19.2).
class PublishCheck {
  const PublishCheck({
    required this.makerChecker,
    required this.canRequest,
    required this.canPublishDirectly,
    required this.canApprove,
    this.openRequestId,
    this.openRequestStatus,
    this.meReasons = const [],
    this.makers = const [],
    this.publishablePayslips = 0,
    this.held = 0,
  });
  final bool makerChecker;
  final bool canRequest;
  final bool canPublishDirectly;
  final bool canApprove;
  final String? openRequestId;
  final String? openRequestStatus;

  /// Why the caller is a maker (empty = not a maker).
  final List<String> meReasons;
  final List<PublishMaker> makers;
  final int publishablePayslips;
  final int held;

  bool get hasOpenRequest => openRequestId != null;
  bool get callerIsMaker => meReasons.isNotEmpty;

  factory PublishCheck.fromJson(Map<String, dynamic> j) {
    final open = j['open_request'] is Map ? _m(j['open_request']) : null;
    final pub = _m(j['publishable']);
    return PublishCheck(
      makerChecker: j['maker_checker'] == true,
      canRequest: j['can_request'] == true,
      canPublishDirectly: j['can_publish_directly'] == true,
      canApprove: j['can_approve'] == true,
      openRequestId: open == null ? null : _opt(open['id']),
      openRequestStatus: open == null ? null : _opt(open['status']),
      meReasons: _strings(_m(j['me'])['reasons']),
      makers: [
        for (final e in _list(j['makers']))
          PublishMaker(user: _user(e['user']), reasons: _strings(e['reasons'])),
      ],
      publishablePayslips: _i(pub['payslips']),
      held: _i(pub['held']),
    );
  }
}

/// Whether [selfId] may approve or reject [r]: not the requester, not a known
/// maker, not the employee of a payslip request, and (when the server's
/// publish-check is known) not a maker and allowed to approve. Cosmetic: the
/// server enforces it (403 SECOND_PERSON_REQUIRED / FORBIDDEN).
bool canDecidePublishRequest(PublishRequest r, String? selfId, {PublishCheck? check}) {
  if (selfId == null || selfId.isEmpty || !r.isSubmitted) return false;
  if (selfId == r.requestedBy.id) return false;
  if (r.isPayslip && selfId == r.payslipUser?.id) return false;
  if (r.makers?.any((m) => m.id == selfId) ?? false) return false;
  if (check != null && (check.callerIsMaker || !check.canApprove)) return false;
  return true;
}

String? cannotDecidePublishReason(PublishRequest r, String? selfId, {PublishCheck? check}) {
  if (!r.isSubmitted || selfId == null || canDecidePublishRequest(r, selfId, check: check)) return null;
  if (r.isPayslip && selfId == r.payslipUser?.id) {
    return 'This is your own payslip. You cannot decide on it.';
  }
  if (selfId == r.requestedBy.id) return 'You asked for this publish. A different person must decide.';
  if (check != null && check.callerIsMaker) {
    return 'You ${check.meReasons.map(publishReasonLabel).join(', ')}. A different person must decide.';
  }
  return 'You cannot decide on this request. A different person with publish rights must.';
}

String publishRequestScopeLabel(PublishRequest r) => r.isPayslip
    ? 'Payslip of ${r.payslipUser?.display ?? 'an employee'} for ${periodTitle(r.runPeriod)}'
    : 'Payroll run of ${periodTitle(r.runPeriod)}';

/// Short text for the newest request on a run or payslip.
String publishRefLine(PublishRequestRef r) {
  final by = r.requestedBy?.fullName.isNotEmpty ?? false ? ' by ${r.requestedBy!.fullName}' : '';
  return switch (r.status) {
    'submitted' => 'Publish requested$by. Waiting for a second person.',
    'approved' => 'Publish request approved.',
    'rejected' => 'Publish request rejected.',
    'withdrawn' => 'Publish request withdrawn.',
    'stale' => 'Publish request out of date. ${publishStaleLabel(r.staleReason)}',
    _ => publishStatusLabel(r.status),
  };
}

// ---- delivery (19.3) ---------------------------------------------------------------------

class DeliveryCounts {
  const DeliveryCounts({this.published = 0, this.pushed = 0, this.viewed = 0, this.downloaded = 0, this.noDevice = 0});
  final int published;
  final int pushed;
  final int viewed;
  final int downloaded;
  final int noDevice;

  factory DeliveryCounts.fromJson(Map<String, dynamic> j) => DeliveryCounts(
        published: _i(j['published']),
        pushed: _i(j['pushed']),
        viewed: _i(j['viewed']),
        downloaded: _i(j['downloaded']),
        noDevice: _i(j['no_device']),
      );
}

class DeliveryItem {
  const DeliveryItem({
    required this.user,
    required this.payslipId,
    this.revision = 1,
    this.pushedAt,
    this.pushCount = 0,
    this.reminders = 0,
    this.firstViewedAt,
    this.firstDownloadedAt,
    this.hasDevice = true,
  });
  final EmployeeRef user;
  final String payslipId;
  final int revision;
  final DateTime? pushedAt;
  final int pushCount;
  final int reminders;
  final DateTime? firstViewedAt;
  final DateTime? firstDownloadedAt;
  final bool hasDevice;

  bool get viewed => firstViewedAt != null;

  /// Cosmetic: the server skips VIEWED, NO_DEVICE and LIMIT_REACHED itself.
  bool get canRemind => !viewed && hasDevice && reminders < deliveryMaxReminders;

  factory DeliveryItem.fromJson(Map<String, dynamic> j) => DeliveryItem(
        user: _user(j['user']),
        payslipId: _s(j['payslip_id']),
        revision: _i(j['revision']) == 0 ? 1 : _i(j['revision']),
        pushedAt: parseTimestamp(j['pushed_at']),
        pushCount: _i(j['push_count']),
        reminders: _i(j['reminders']),
        firstViewedAt: parseTimestamp(j['first_viewed_at']),
        firstDownloadedAt: parseTimestamp(j['first_downloaded_at']),
        hasDevice: j['has_device'] != false,
      );
}

const int deliveryMaxReminders = 3;

/// Filter of the delivery report. Value equality so it can key a provider.
class DeliveryQuery {
  const DeliveryQuery(this.runId, {this.state, this.q = ''});
  final String runId;

  /// `viewed` | `not_viewed` | `no_device` | null.
  final String? state;
  final String q;

  @override
  bool operator ==(Object other) =>
      other is DeliveryQuery && other.runId == runId && other.state == state && other.q == q;

  @override
  int get hashCode => Object.hash(runId, state, q);
}

/// One page of the delivery report with the run-wide counts.
class DeliveryPage {
  const DeliveryPage({required this.counts, required this.items});
  final DeliveryCounts counts;
  final Paged<DeliveryItem> items;

  factory DeliveryPage.fromJson(Map<String, dynamic> j) => DeliveryPage(
        counts: DeliveryCounts.fromJson(_m(j['counts'])),
        items: Paged.parse(j, DeliveryItem.fromJson),
      );
}

const Map<String, String> remindSkipLabels = {
  'VIEWED': 'already opened it',
  'NO_DEVICE': 'no registered phone',
  'TOO_SOON': 'reminded in the last 24 hours',
  'LIMIT_REACHED': 'already reminded 3 times',
  'NOT_PUBLISHED': 'payslip not published',
};

class RemindResult {
  const RemindResult({required this.sent, this.skipped = const []});
  final int sent;
  final List<({String userId, String reason})> skipped;

  factory RemindResult.fromJson(Map<String, dynamic> j) => RemindResult(
        sent: _i(j['sent']),
        skipped: [
          for (final e in _list(j['skipped'])) (userId: _s(e['user_id']), reason: _s(e['reason'])),
        ],
      );

  /// "14 reminders sent. 2 skipped: 1 already opened it, 1 no registered phone."
  String summary() {
    final b = StringBuffer(sent == 1 ? '1 reminder sent.' : '$sent reminders sent.');
    if (skipped.isNotEmpty) {
      final counts = <String, int>{};
      for (final s in skipped) {
        counts[s.reason] = (counts[s.reason] ?? 0) + 1;
      }
      b.write(' ${skipped.length} skipped: ');
      b.write([for (final e in counts.entries) '${e.value} ${remindSkipLabels[e.key] ?? e.key}'].join(', '));
      b.write('.');
    }
    return b.toString();
  }
}

// ---- company bank accounts (19.4) --------------------------------------------------------

class BankAccount {
  const BankAccount({
    required this.id,
    required this.label,
    required this.bankName,
    required this.account,
    required this.ifsc,
    required this.isDefault,
    required this.active,
    this.createdAt,
    this.createdBy,
  });
  final String id;
  final String label;
  final String bankName;

  /// Always masked by the server.
  final String account;
  final String ifsc;
  final bool isDefault;
  final bool active;
  final DateTime? createdAt;
  final EmployeeRef? createdBy;

  factory BankAccount.fromJson(Map<String, dynamic> j) => BankAccount(
        id: _s(j['id']),
        label: _s(j['label']),
        bankName: _s(j['bank_name']),
        account: _s(j['account']),
        ifsc: _s(j['ifsc']),
        isDefault: j['is_default'] == true,
        active: j['active'] != false,
        createdAt: parseTimestamp(j['created_at']),
        createdBy: EmployeeRef.parse(j['created_by']),
      );

  static List<BankAccount> parseList(Map<String, dynamic> j) =>
      [for (final e in _list(j['items'])) BankAccount.fromJson(e)];
}

/// New company account. The unmasked number is typed once and sent once; the
/// object is dropped by the form afterwards.
class BankAccountInput {
  const BankAccountInput({
    required this.label,
    required this.bankName,
    required this.account,
    required this.ifsc,
    this.isDefault = false,
  });
  final String label;
  final String bankName;
  final String account;
  final String ifsc;
  final bool isDefault;

  Map<String, Object?> toJson(String currentPassword) => {
        'label': label.trim(),
        'bank_name': bankName.trim(),
        'account': account.trim(),
        'ifsc': ifsc.trim().toUpperCase(),
        'is_default': isDefault,
        'current_password': currentPassword,
      };
}

String? validateBankLabel(String? v) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return 'Required';
  if (t.length > 40) return 'At most 40 characters';
  return null;
}

String? validateBankName(String? v) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return 'Required';
  if (t.length > 80) return 'At most 80 characters';
  return null;
}

String? validateDebitAccount(String? v) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return 'Required';
  if (!RegExp(r'^\d{9,18}$').hasMatch(t)) return 'Enter 9 to 18 digits';
  return null;
}

String? validateDebitIfsc(String? v) {
  final t = (v ?? '').trim().toUpperCase();
  if (t.isEmpty) return 'Required';
  if (!RegExp(r'^[A-Z]{4}0[A-Z0-9]{6}$').hasMatch(t)) {
    return 'IFSC is 11 characters: 4 letters, a zero, then 6 letters or digits';
  }
  return null;
}

// ---- payment batches (19.5) --------------------------------------------------------------

const Map<String, String> batchStatusLabels = {
  'draft': 'Draft',
  'submitted': 'Waiting for approval',
  'approved': 'Approved',
  'exported': 'File downloaded',
  'closed': 'Closed',
  'cancelled': 'Cancelled',
};

String batchStatusLabel(String s) => batchStatusLabels[s] ?? s;

const List<String> batchStatuses = ['draft', 'submitted', 'approved', 'exported', 'closed', 'cancelled'];

const Map<String, String> lineStatusLabels = {
  'pending': 'Pending',
  'excluded': 'Left out',
  'exported': 'In the file',
  'paid': 'Paid',
  'failed': 'Failed',
};

String lineStatusLabel(String s) => lineStatusLabels[s] ?? s;

const Map<String, String> lineFlagLabels = {
  'BANK_DETAILS_CHANGED': 'Bank details changed since the payslip was published',
  'RECENT_BANK_CHANGE': 'Bank details changed recently',
  'REVISION_DELTA': 'Pays only the difference of a revised payslip',
  'NO_PAN': 'No PAN on record',
};

String lineFlagLabel(String f) => lineFlagLabels[f] ?? f;

const Map<String, String> blockerLabels = {
  'NO_BANK_DETAILS': 'No bank details',
  'INVALID_ACCOUNT': 'Account number is not valid',
  'INVALID_IFSC': 'IFSC is not valid',
  'NAME': 'Name has no usable letters for the bank file',
  'NOT_BANK_TRANSFER': 'Paid by cash or cheque, not bank transfer',
  'HELD': 'Payslip is on hold',
  'ZERO_NET': 'Nothing to pay (net is zero)',
  'ALREADY_PAID': 'Already paid',
  'IN_OTHER_BATCH': 'Already in another payment batch',
  'RECOVERY': 'The revised payslip is lower than what was paid (recover separately)',
};

String blockerLabel(String r) => blockerLabels[r] ?? r;

const Map<String, String> staleLineLabels = {
  'PAYSLIP_SUPERSEDED': 'the payslip was revised',
  'HELD': 'the payslip is on hold',
  'AMOUNT_CHANGED': 'the amount changed',
  'BANK_DETAILS_CHANGED': 'the bank details changed',
};

class DebitAccountRef {
  const DebitAccountRef({required this.id, required this.label, required this.account});
  final String id;
  final String label;

  /// Masked.
  final String account;

  static DebitAccountRef? parse(Object? v) {
    if (v is! Map) return null;
    final j = v.cast<String, dynamic>();
    return DebitAccountRef(id: _s(j['id']), label: _s(j['label']), account: _s(j['account']));
  }
}

class BatchCounts {
  const BatchCounts({this.lines = 0, this.excluded = 0, this.exported = 0, this.paid = 0, this.failed = 0});
  final int lines;
  final int excluded;
  final int exported;
  final int paid;
  final int failed;

  factory BatchCounts.fromJson(Map<String, dynamic> j) => BatchCounts(
        lines: _i(j['lines']),
        excluded: _i(j['excluded']),
        exported: _i(j['exported']),
        paid: _i(j['paid']),
        failed: _i(j['failed']),
      );
}

/// One employee line. Account and IFSC are the snapshot the publish reviewed;
/// [currentAccount] / [currentIfsc] are set only for BANK_DETAILS_CHANGED lines.
/// All masked by the server.
class PaymentLine {
  const PaymentLine({
    required this.id,
    required this.seq,
    required this.user,
    required this.amount,
    required this.beneficiaryName,
    required this.account,
    required this.ifsc,
    required this.accountSource,
    required this.status,
    required this.version,
    this.payslipId = '',
    this.payslipRevision = 1,
    this.currentAccount,
    this.currentIfsc,
    this.flags = const [],
    this.reference,
    this.paidOn,
  });
  final String id;
  final int seq;
  final EmployeeRef user;
  final String payslipId;
  final int payslipRevision;
  final String amount;
  final String beneficiaryName;
  final String account;
  final String ifsc;

  /// `snapshot` | `current`.
  final String accountSource;
  final String? currentAccount;
  final String? currentIfsc;
  final List<String> flags;
  final String status;
  final String? reference;
  final String? paidOn;
  final int version;

  bool get bankChanged => flags.contains('BANK_DETAILS_CHANGED');
  bool get usesCurrent => accountSource == 'current';
  bool get isExcluded => status == 'excluded';
  bool get isPending => status == 'pending';
  bool get isExported => status == 'exported';

  factory PaymentLine.fromJson(Map<String, dynamic> j) {
    final cur = j['current'] is Map ? _m(j['current']) : null;
    final slip = _m(j['payslip']);
    return PaymentLine(
      id: _s(j['id']),
      seq: _i(j['seq']),
      user: _user(j['user']),
      payslipId: _s(slip['id']),
      payslipRevision: _i(slip['revision']) == 0 ? 1 : _i(slip['revision']),
      amount: _s(j['amount']),
      beneficiaryName: _s(j['beneficiary_name']),
      account: _s(j['account']),
      ifsc: _s(j['ifsc']),
      accountSource: _opt(j['account_source']) ?? 'snapshot',
      currentAccount: cur == null ? null : _opt(cur['account']),
      currentIfsc: cur == null ? null : _opt(cur['ifsc']),
      flags: _strings(j['flags']),
      status: _opt(j['status']) ?? 'pending',
      reference: _opt(j['reference']),
      paidOn: _opt(j['paid_on']),
      version: _i(j['version']),
    );
  }
}

class PaymentBlocker {
  const PaymentBlocker({required this.user, required this.reason, this.payslipId = ''});
  final EmployeeRef user;
  final String payslipId;
  final String reason;

  factory PaymentBlocker.fromJson(Map<String, dynamic> j) => PaymentBlocker(
        user: _user(j['user']),
        payslipId: _s(j['payslip_id']),
        reason: _s(j['reason']),
      );
}

class PaymentBatch {
  const PaymentBatch({
    required this.id,
    required this.reference,
    required this.runId,
    required this.runPeriod,
    required this.status,
    required this.counts,
    required this.total,
    required this.version,
    this.debitAccount,
    this.valueDate,
    this.format = 'generic_neft_csv',
    this.lines = const [],
    this.blockers = const [],
    this.fileSha256,
    this.exportedAt,
    this.exportedBy,
    this.createdBy,
    this.createdAt,
    this.submittedBy,
    this.submittedAt,
    this.decidedBy,
    this.decidedAt,
    this.decisionComment,
  });
  final String id;
  final String reference;
  final String runId;
  final String runPeriod;
  final String status;
  final DebitAccountRef? debitAccount;
  final String? valueDate;
  final String format;
  final BatchCounts counts;
  final String total;
  final List<PaymentLine> lines;
  final List<PaymentBlocker> blockers;
  final String? fileSha256;
  final DateTime? exportedAt;
  final EmployeeRef? exportedBy;
  final EmployeeRef? createdBy;
  final DateTime? createdAt;
  final EmployeeRef? submittedBy;
  final DateTime? submittedAt;
  final EmployeeRef? decidedBy;
  final DateTime? decidedAt;
  final String? decisionComment;
  final int version;

  bool get isDraft => status == 'draft';
  bool get isSubmitted => status == 'submitted';
  bool get isApproved => status == 'approved';
  bool get isExported => status == 'exported';
  bool get isClosed => status == 'closed';
  bool get isCancelled => status == 'cancelled';

  /// The file can be (re)downloaded.
  bool get hasFile => isApproved || isExported || isClosed;
  bool get canCancel => isDraft || isSubmitted || isApproved;

  factory PaymentBatch.fromJson(Map<String, dynamic> j) {
    final run = _m(j['run']);
    return PaymentBatch(
      id: _s(j['id']),
      reference: _s(j['reference']),
      runId: _s(run['id']),
      runPeriod: _s(run['period']),
      status: _opt(j['status']) ?? 'draft',
      debitAccount: DebitAccountRef.parse(j['debit_account']),
      valueDate: _opt(j['value_date']),
      format: _opt(j['format']) ?? 'generic_neft_csv',
      counts: BatchCounts.fromJson(_m(j['counts'])),
      total: _s(j['total']).isEmpty ? '0.00' : _s(j['total']),
      lines: [for (final e in _list(j['lines'])) PaymentLine.fromJson(e)],
      blockers: [for (final e in _list(j['blockers'])) PaymentBlocker.fromJson(e)],
      fileSha256: _opt(j['file_sha256']),
      exportedAt: parseTimestamp(j['exported_at']),
      exportedBy: EmployeeRef.parse(j['exported_by']),
      createdBy: EmployeeRef.parse(j['created_by']),
      createdAt: parseTimestamp(j['created_at']),
      submittedBy: EmployeeRef.parse(j['submitted_by']),
      submittedAt: parseTimestamp(j['submitted_at']),
      decidedBy: EmployeeRef.parse(j['decided_by']),
      decidedAt: parseTimestamp(j['decided_at']),
      decisionComment: _opt(j['decision_comment']),
      version: _i(j['version']) == 0 ? 1 : _i(j['version']),
    );
  }
}

class PaymentBatchQuery {
  const PaymentBatchQuery({this.status, this.runId});
  final String? status;
  final String? runId;

  @override
  bool operator ==(Object other) =>
      other is PaymentBatchQuery && other.status == status && other.runId == runId;

  @override
  int get hashCode => Object.hash(status, runId);
}

/// Body of `POST .../payment-batches`.
class PaymentBatchCreate {
  const PaymentBatchCreate({required this.valueDate, this.debitAccountId, this.userIds});
  final String valueDate;
  final String? debitAccountId;
  final List<String>? userIds;

  Map<String, Object?> toJson() => {
        'debit_account_id': debitAccountId,
        'value_date': valueDate,
        'user_ids': userIds,
      };
}

/// Body of `PUT .../payment-batches/{id}`. Only [expectedVersion] = refresh
/// the stale lines of the draft.
class PaymentBatchEdit {
  const PaymentBatchEdit({
    this.valueDate,
    this.debitAccountId,
    this.excludeLineIds = const [],
    this.includeLineIds = const [],
    this.currentAccountLineIds = const [],
  });
  final String? valueDate;
  final String? debitAccountId;
  final List<String> excludeLineIds;
  final List<String> includeLineIds;

  /// Lines whose file entry uses the employee's current account.
  final List<String> currentAccountLineIds;

  Map<String, Object?> toJson(int expectedVersion) => {
        'expected_version': expectedVersion,
        if (valueDate != null) 'value_date': valueDate,
        if (debitAccountId != null) 'debit_account_id': debitAccountId,
        if (excludeLineIds.isNotEmpty) 'exclude_line_ids': excludeLineIds,
        if (includeLineIds.isNotEmpty) 'include_line_ids': includeLineIds,
        if (currentAccountLineIds.isNotEmpty)
          'account_sources': [
            for (final id in currentAccountLineIds) {'line_id': id, 'source': 'current'},
          ],
      };
}

/// One line of the confirm call.
class ConfirmLine {
  const ConfirmLine({required this.lineId, required this.status, this.reference});
  final String lineId;

  /// `paid` | `failed`.
  final String status;
  final String? reference;

  Map<String, Object?> toJson() => {
        'line_id': lineId,
        'status': status,
        if (reference != null && reference!.trim().isNotEmpty) 'reference': reference!.trim(),
      };
}

/// Body of `POST .../confirm` without the password.
class PaymentConfirm {
  const PaymentConfirm({required this.paidOn, this.lines, this.allPaid = false, this.reference});
  final String paidOn;
  final List<ConfirmLine>? lines;
  final bool allPaid;
  final String? reference;

  Map<String, Object?> toJson(String currentPassword) => {
        'paid_on': paidOn,
        'lines': lines == null ? null : [for (final l in lines!) l.toJson()],
        'all_paid': allPaid,
        'reference': reference == null || reference!.trim().isEmpty ? null : reference!.trim(),
        'current_password': currentPassword,
      };
}

// ---- rules and validation ------------------------------------------------------------------

/// Whether [selfId] may approve or reject [b]: not its creator or submitter,
/// not the person who sent it back, and not an employee who is paid by it.
/// Cosmetic: the server also blocks anybody who changed the bank details of a
/// paid employee recently (403 SECOND_PERSON_REQUIRED) and a line that pays
/// the caller (403 FORBIDDEN).
bool canDecideBatch(PaymentBatch b, String? selfId) {
  if (selfId == null || selfId.isEmpty || !b.isSubmitted) return false;
  if (selfId == b.createdBy?.id || selfId == b.submittedBy?.id) return false;
  if (b.lines.any((l) => l.user.id == selfId && !l.isExcluded)) return false;
  return true;
}

String? cannotDecideBatchReason(PaymentBatch b, String? selfId) {
  if (!b.isSubmitted || selfId == null || canDecideBatch(b, selfId)) return null;
  if (b.lines.any((l) => l.user.id == selfId && !l.isExcluded)) {
    return 'This batch pays you. You cannot decide on it.';
  }
  return 'You made or submitted this batch. A different person must approve or reject it.';
}

/// After a rejection the person who rejected it (a checker) must not also
/// edit or submit it again. Cosmetic.
bool canEditBatch(PaymentBatch b, String? selfId) =>
    b.isDraft && (b.decidedBy == null || b.decidedBy!.id != selfId);

/// `YYYY-MM-DD` value date: today to today + 30 days (19.5).
String? validateValueDate(DateTime? d, DateTime today) {
  if (d == null) return 'Choose the value date';
  final t = DateTime(today.year, today.month, today.day);
  final v = DateTime(d.year, d.month, d.day);
  if (v.isBefore(t)) return 'The value date cannot be in the past';
  if (v.isAfter(t.add(const Duration(days: 30)))) return 'The value date can be at most 30 days ahead';
  return null;
}

/// Payment date: not in the future and not before the day the file was downloaded.
String? validatePaidOn(DateTime? d, DateTime today, {DateTime? exportedAt}) {
  if (d == null) return 'Choose the payment date';
  final t = DateTime(today.year, today.month, today.day);
  final v = DateTime(d.year, d.month, d.day);
  if (v.isAfter(t)) return 'The payment date cannot be in the future';
  if (exportedAt != null) {
    final e = DateTime(exportedAt.year, exportedAt.month, exportedAt.day);
    if (v.isBefore(e)) return 'The payment date cannot be before the file was downloaded';
  }
  return null;
}

/// Bank reference (UTR): optional, 1 to 40 of letters, digits, space . _ / -.
String? validateBankReference(String? v) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return null;
  if (t.length > 40) return 'At most 40 characters';
  if (!RegExp(r'^[A-Za-z0-9 ._/-]+$').hasMatch(t)) return 'Letters, digits, space and . _ / - only';
  return null;
}

/// Cap on the lines of one batch (19.5).
const int maxBatchLines = 1000;

// ---- errors ----------------------------------------------------------------------------------

/// `NOTHING_TO_PAY` carries `details: [{field: <employee id>, message: <blocker code>}]`.
List<String> nothingToPayReasons(ApiException e) {
  final counts = <String, int>{};
  for (final d in e.details) {
    if (d.message.isNotEmpty) counts[d.message] = (counts[d.message] ?? 0) + 1;
  }
  return [for (final c in counts.entries) '${c.value} x ${blockerLabel(c.key)}'];
}

/// `BATCH_STALE` carries `details: [{field: "lines[<seq>]", message: <code>}]`.
List<String> staleLineReasons(ApiException e) => [
      for (final d in e.details)
        if (RegExp(r'\[(\d+)\]').firstMatch(d.field) case final m?)
          'Line ${m[1]}: ${staleLineLabels[d.message] ?? d.message}',
    ];

/// Plain-language text for the phase 5a codes; everything else falls through to
/// [payrollErrorMessage]. Never includes values from the request.
String phase5ErrorMessage(ApiException e) {
  switch (e.code) {
    case 'PUBLISH_REQUEST_REQUIRED':
      return 'Publishing needs a second person. Send a publish request; somebody else with publish rights approves it.';
    case 'PUBLISH_REQUEST_EXISTS':
      return 'A publish request is already waiting for this. Open it instead of sending another.';
    case 'MAKER_CHECKER_OFF':
      return 'Second-person approval for publishing is switched off, so no request is needed. Publish directly.';
    case 'PUBLISH_STALE':
      return 'The payroll changed, or the request expired, after it was sent. The request is closed: '
          'ask for a new one.';
    case 'COMPONENTS_CHANGED':
      return 'A pay item changed after the run was processed. Nothing was published. '
          'Reopen the run, refresh it and process it again.';
    case 'NOT_SUBMITTED':
      return 'This is no longer waiting for approval. Reload to see its state.';
    case 'NOT_DRAFT':
      return 'Only a draft batch can be changed, and a batch can only be cancelled before its file is downloaded. '
          'Reload to see its state.';
    case 'NOT_APPROVED':
      return 'The batch is not approved, or its file was not downloaded yet, so this step is not available. '
          'Reload to see its state.';
    case 'BATCH_STALE':
      final lines = staleLineReasons(e);
      return 'The payslips or bank details changed after this batch was made. '
          'Use "Refresh lines" and check the result.${lines.isEmpty ? '' : '\n${lines.join('\n')}'}';
    case 'NOTHING_TO_PAY':
      final why = nothingToPayReasons(e);
      return 'There is nothing to pay.${why.isEmpty ? '' : ' Reasons: ${why.join(', ')}.'}';
    case 'IN_USE':
      return 'This account is used by a payment batch that is still a draft, waiting or approved. '
          'Cancel or finish that batch first.';
    case 'TOO_MANY_ROWS':
      return 'A payment batch can have at most $maxBatchLines lines. Choose fewer employees.';
    case 'INVALID_DATE':
      return 'Check the date. The value date is today up to 30 days ahead. The payment date is not in the future '
          'and not before the day the file was downloaded.';
    case 'SECOND_PERSON_REQUIRED':
      return 'A different person must do this. You asked for it, made, edited or submitted it, '
          'or recently changed the bank details of someone paid by it, so you cannot approve or reject it.';
    case 'FORBIDDEN':
      return 'You cannot decide on your own pay. A different person must do this.';
    case 'CONFLICT':
      return 'This changed or is already in use: a payslip may already be in another open payment line. '
          'Reload to see the current state.';
    default:
      return payrollErrorMessage(e);
  }
}

/// Message for any error thrown by a phase 5a call.
String p5Msg(Object e) => e is ApiException ? phase5ErrorMessage(e) : 'Something went wrong. Please try again.';

/// Codes after which the shown request or batch is out of date and is reloaded.
bool isStalePhase5Error(ApiException e) => const {
      'VERSION_CONFLICT',
      'NOT_SUBMITTED',
      'NOT_DRAFT',
      'NOT_APPROVED',
      'PUBLISH_STALE',
      'BATCH_STALE',
      'PAYROLL_LOCKED',
      'CONFLICT',
    }.contains(e.code);

// ---- dashboard alerts ------------------------------------------------------------------------

const Map<String, String> phase5AlertText = {
  'PUBLISH_PENDING_APPROVAL': 'A publish request is waiting for approval.',
  'PAYMENT_NOT_EXPORTED': 'The pay date has passed and the salary has not been sent to the bank yet (no payment file downloaded).',
  'PAYMENT_FAILED_LINES': 'Some bank payments failed. Check the payment batches and pay them again.',
};
