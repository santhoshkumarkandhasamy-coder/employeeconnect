import 'dart:typed_data';

import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart' show Paged;
import 'package:employee_connect/features/payroll/data/payroll_repository.dart';
import 'package:employee_connect/features/payroll/domain/payroll_models.dart';
import 'package:employee_connect/features/payroll/domain/phase4a_models.dart' show Increment, IncrementBatch, IncrementQuery;
import 'package:employee_connect/features/payroll/domain/phase5a_models.dart';
import 'package:employee_connect/features/payroll/presentation/approvals_tab.dart';
import 'package:employee_connect/features/payroll/presentation/bank_accounts_section.dart';
import 'package:employee_connect/features/payroll/presentation/dashboard_tab.dart';
import 'package:employee_connect/features/payroll/presentation/delivery_report_screen.dart';
import 'package:employee_connect/features/payroll/presentation/payment_batch_screen.dart';
import 'package:employee_connect/features/payroll/presentation/payment_batches_screen.dart';
import 'package:employee_connect/features/payroll/presentation/payments_tab.dart';
import 'package:employee_connect/features/payroll/presentation/payroll_home_screen.dart';
import 'package:employee_connect/features/payroll/presentation/payroll_settings_tab.dart';
import 'package:employee_connect/features/payroll/presentation/publish_request_widgets.dart';
import 'package:employee_connect/features/payroll/presentation/run_review_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/payroll_fakes.dart';
import 'payroll_phase5a_logic_test.dart' show batchJson, checkJson, lineJson, requestJson, userJson;

class Fake5a extends FakeRepo {
  // publish
  ApiException? publishError;
  PublishCheck check = PublishCheck.fromJson(checkJson());
  PublishRequest request = PublishRequest.fromJson(requestJson());
  List<PublishRequest> requests = [];
  ApiException? createError;
  ApiException? approveError;
  final created = <Map<String, Object?>>[];
  final approvals = <Map<String, Object?>>[];
  final rejects = <Map<String, Object?>>[];
  final withdraws = <int>[];

  // payments
  PaymentBatch batch = PaymentBatch.fromJson(batchJson());
  List<PaymentBatch> batches = [];
  ApiException? batchError;
  final edits = <Map<String, Object?>>[];
  final batchCalls = <String>[];
  final batchApprovals = <Map<String, Object?>>[];
  final files = <String>[];
  final confirms = <Map<String, Object?>>[];
  int filePwRejects = 0;
  ApiException? createBatchError;
  final createdBatches = <Map<String, Object?>>[];

  // accounts and delivery
  List<BankAccount> accounts = [];
  ApiException? accountError;
  final accountCreates = <Map<String, Object?>>[];
  final accountUpdates = <Map<String, Object?>>[];
  final accountPasswords = <String?>[];
  DeliveryPage deliveryPage = DeliveryPage.fromJson({
    'counts': {'published': 3, 'pushed': 3, 'viewed': 1, 'downloaded': 0, 'no_device': 1},
    'items': [
      {'user': userJson('u1', 'Asha Rao'), 'payslip_id': 'p1', 'has_device': true, 'reminders': 0},
      {'user': userJson('u2', 'Ravi Kumar'), 'payslip_id': 'p2', 'has_device': true, 'reminders': 3},
      {'user': userJson('u3', 'Meena S'), 'payslip_id': 'p3', 'has_device': false},
    ],
    'page': 1,
    'page_size': 50,
    'total': 3,
  });
  final reminds = <List<String>?>[];

  @override
  Future<Paged<Increment>> increments(IncrementQuery q, {int page = 1}) async =>
      const Paged(items: [], page: 1, total: 0, pageSize: 20);

  @override
  Future<Paged<IncrementBatch>> incrementBatches({String? kind, int page = 1}) async =>
      const Paged(items: [], page: 1, total: 0, pageSize: 20);

  @override
  Future<PayrollRun> publishRun(String id, {required String currentPassword}) async {
    if (publishError != null) throw publishError!;
    return super.publishRun(id, currentPassword: currentPassword);
  }

  @override
  Future<PublishCheck> publishCheckRun(String runId) async => check;

  @override
  Future<PublishCheck> publishCheckPayslip(String payslipId) async => check;

  @override
  Future<PublishRequest> createRunPublishRequest(String runId, {String? comment}) async {
    created.add({'run': runId, 'comment': comment});
    if (createError != null) throw createError!;
    return request;
  }

  @override
  Future<PublishRequest> createPayslipPublishRequest(String payslipId, {String? comment}) async {
    created.add({'payslip': payslipId, 'comment': comment});
    return request;
  }

  @override
  Future<Paged<PublishRequest>> publishRequests(PublishRequestQuery q, {int page = 1}) async {
    final items = [for (final r in requests) if (q.status == null || r.status == q.status) r];
    return Paged(items: items, page: 1, total: items.length, pageSize: 20);
  }

  @override
  Future<PublishRequest> publishRequest(String id) async => request;

  @override
  Future<PublishRequest> withdrawPublishRequest(String id, {required int expectedVersion}) async {
    withdraws.add(expectedVersion);
    return request;
  }

  @override
  Future<PublishApproveResult> approvePublishRequest(
    String id, {
    required int expectedVersion,
    required String currentPassword,
    String? comment,
  }) async {
    approvals.add({'v': expectedVersion, 'pw': currentPassword, 'comment': comment});
    if (pwRejects > 0) {
      pwRejects--;
      throw const ApiException(code: 'REAUTH_FAILED', message: 'no', status: 403);
    }
    if (approveError != null) throw approveError!;
    return PublishApproveResult.fromJson({
      'request': requestJson(status: 'approved'),
      'run': runJson(status: 'published'),
    });
  }

  @override
  Future<PublishRequest> rejectPublishRequest(
    String id, {
    required int expectedVersion,
    required String comment,
  }) async {
    rejects.add({'v': expectedVersion, 'comment': comment});
    return PublishRequest.fromJson(requestJson(status: 'rejected'));
  }

  // payments
  @override
  Future<Paged<PaymentBatch>> paymentBatches(PaymentBatchQuery q, {int page = 1}) async {
    final items = [for (final b in batches) if (q.status == null || b.status == q.status) b];
    return Paged(items: items, page: 1, total: items.length, pageSize: 20);
  }

  @override
  Future<PaymentBatch> paymentBatch(String id) async => batch;

  @override
  Future<PaymentBatch> createPaymentBatch(String runId, PaymentBatchCreate body) async {
    createdBatches.add({'run': runId, ...body.toJson()});
    if (createBatchError != null) throw createBatchError!;
    return batch;
  }

  @override
  Future<PaymentBatch> updatePaymentBatch(String id, PaymentBatchEdit edit, {required int expectedVersion}) async {
    edits.add(edit.toJson(expectedVersion));
    if (batchError != null) throw batchError!;
    return batch;
  }

  @override
  Future<PaymentBatch> submitPaymentBatch(String id, {required int expectedVersion}) async {
    batchCalls.add('submit:$expectedVersion');
    if (batchError != null) throw batchError!;
    return batch;
  }

  @override
  Future<PaymentBatch> withdrawPaymentBatch(String id, {required int expectedVersion}) async {
    batchCalls.add('withdraw:$expectedVersion');
    return batch;
  }

  @override
  Future<PaymentBatch> approvePaymentBatch(
    String id, {
    required int expectedVersion,
    required String currentPassword,
    String? comment,
  }) async {
    batchApprovals.add({'v': expectedVersion, 'pw': currentPassword, 'comment': comment});
    if (batchError != null) throw batchError!;
    return batch;
  }

  @override
  Future<PaymentBatch> rejectPaymentBatch(String id, {required int expectedVersion, required String comment}) async {
    batchCalls.add('reject:$expectedVersion:$comment');
    return batch;
  }

  @override
  Future<PaymentBatch> cancelPaymentBatch(String id, String reason) async {
    batchCalls.add('cancel:$reason');
    return batch;
  }

  @override
  Future<PayrollFile> paymentFile(String id, {required String currentPassword}) async {
    files.add(currentPassword);
    if (filePwRejects > 0) {
      filePwRejects--;
      throw const ApiException(code: 'REAUTH_FAILED', message: 'no', status: 403);
    }
    return PayrollFile(Uint8List.fromList([83, 114]), 'bank-PAY-2026-10-01.csv');
  }

  @override
  Future<PaymentBatch> confirmPayment(String id, PaymentConfirm body, {required String currentPassword}) async {
    confirms.add(body.toJson(currentPassword));
    return batch;
  }

  // accounts, delivery
  @override
  Future<List<BankAccount>> bankAccounts() async => accounts;

  @override
  Future<BankAccount> createBankAccount(BankAccountInput input, {required String currentPassword}) async {
    accountCreates.add(input.toJson(currentPassword));
    if (accountError != null) throw accountError!;
    return accounts.first;
  }

  @override
  Future<BankAccount> updateBankAccount(
    String id, {
    String? label,
    bool? isDefault,
    bool? active,
    String? currentPassword,
  }) async {
    accountUpdates.add({'id': id, 'label': label, 'is_default': isDefault, 'active': active});
    accountPasswords.add(currentPassword);
    if (accountError != null) throw accountError!;
    return accounts.first;
  }

  @override
  Future<DeliveryPage> delivery(DeliveryQuery q, {int page = 1}) async => deliveryPage;

  @override
  Future<RemindResult> remindDelivery(String runId, {List<String>? userIds}) async {
    reminds.add(userIds);
    return const RemindResult(sent: 1, skipped: [(userId: 'u9', reason: 'VIEWED')]);
  }
}

const _manage = ['payroll.view_all', 'payroll.manage'];
const _publish = ['payroll.view_all', 'payroll.publish'];
const _approve = ['payroll.view_all', 'payroll.approve'];
const _all = ['payroll.view_all', 'payroll.manage', 'payroll.publish', 'payroll.approve'];

BankAccount _account({String id = 'ba1', bool isDefault = true, bool active = true}) => BankAccount(
      id: id,
      label: 'HDFC salary account',
      bankName: 'HDFC Bank',
      account: 'XXXXXX4321',
      ifsc: 'HDFC0001234',
      isDefault: isDefault,
      active: active,
    );

Future<void> _tap(WidgetTester tester, String key) async {
  final f = find.byKey(ValueKey(key));
  await tester.ensureVisible(f);
  await tester.pumpAndSettle();
  await tester.tap(f);
  await tester.pumpAndSettle();
}

/// Taps a button that shows a spinner while the password prompt is open
/// (pumpAndSettle would never finish).
Future<void> _tapBusy(WidgetTester tester, String key) async {
  final f = find.byKey(ValueKey(key));
  await tester.ensureVisible(f);
  await tester.pumpAndSettle();
  await tester.tap(f);
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 600));
}

Future<void> _password(WidgetTester tester, String pw) async {
  expect(find.byKey(const ValueKey('reauth-password')), findsOneWidget);
  await tester.enterText(find.byKey(const ValueKey('reauth-password')), pw);
  await tester.tap(find.text('Confirm'));
  await tester.pumpAndSettle();
}

Future<void> _comment(WidgetTester tester, String text) async {
  if (text.isNotEmpty) await tester.enterText(find.byKey(const ValueKey('comment-field')), text);
  await tester.tap(find.byKey(const ValueKey('comment-confirm')));
  await tester.pumpAndSettle();
}

PublishRequest _req({String status = 'submitted', String requester = 'maker', String scope = 'run', String? stale}) =>
    PublishRequest.fromJson(requestJson(status: status, requester: requester, scope: scope, staleReason: stale));

PaymentBatch _batch({
  String status = 'draft',
  String creator = 'maker',
  String? submitter,
  String? decider,
  List<Map<String, dynamic>>? lines,
  List<Map<String, dynamic>> blockers = const [],
  String? exportedAt,
  int version = 4,
}) =>
    PaymentBatch.fromJson(batchJson(
      status: status,
      creator: creator,
      submitter: submitter,
      decider: decider,
      lines: lines,
      blockers: blockers,
      exportedAt: exportedAt,
      version: version,
    ),);

void main() {
  group('run review: publish requests', () {
    testWidgets('direct publish refused with 409 offers the request flow, then sends the request', (tester) async {
      final repo = Fake5a()
        ..run0 = PayrollRun.fromJson(runJson(status: 'processed'))
        ..publishError = const ApiException(code: 'PUBLISH_REQUEST_REQUIRED', message: 'x', status: 409);
      await pumpPayroll(tester, const RunReviewScreen(runId: 'r1'), repo: repo, perms: _all);
      await _tap(tester, 'publish-run');
      await tester.tap(find.text('Publish').last);
      await tester.pumpAndSettle();
      await _password(tester, 'pw');
      // not a plain error: the second-person flow is offered
      expect(find.text('A second person must approve'), findsOneWidget);
      await tester.tap(find.text('Request publish').last);
      await tester.pumpAndSettle();
      expect(find.textContaining('Ask for approval to publish 46 payslips'), findsOneWidget);
      await _comment(tester, 'Month end');
      expect(repo.created, [
        {'run': 'r1', 'comment': 'Month end'},
      ]);
      expect(find.textContaining('Publish request sent'), findsOneWidget);
    });

    testWidgets('a manager without publish rights requests directly; no password is asked', (tester) async {
      final repo = Fake5a()..run0 = PayrollRun.fromJson(runJson(status: 'processed'));
      await pumpPayroll(tester, const RunReviewScreen(runId: 'r1'), repo: repo, perms: _manage);
      expect(find.byKey(const ValueKey('publish-run')), findsNothing);
      await _tap(tester, 'request-publish');
      await _comment(tester, '');
      expect(repo.created.single['comment'], anyOf(isNull, isEmpty));
      expect(find.text('Confirm your password'), findsNothing);
    });

    testWidgets('a waiting request is shown with its status and hides the request button', (tester) async {
      final repo = Fake5a()
        ..run0 = PayrollRun.fromJson({
          ...runJson(status: 'processed'),
          'publish_request': {
            'id': 'pr1',
            'status': 'submitted',
            'requested_by': userJson('maker', 'Meena'),
            'requested_at': '2026-10-30T10:00:00Z',
            'stale_reason': null,
          },
        });
      await pumpPayroll(tester, const RunReviewScreen(runId: 'r1'), repo: repo, perms: _manage);
      expect(find.byKey(const ValueKey('publish-ref-card')), findsOneWidget);
      expect(find.textContaining('Waiting for a second person'), findsOneWidget);
      expect(find.text('Waiting for approval'), findsOneWidget);
      expect(find.byKey(const ValueKey('request-publish')), findsNothing);
    });

    testWidgets('a stale request shows why and allows a new one', (tester) async {
      final repo = Fake5a()
        ..run0 = PayrollRun.fromJson({
          ...runJson(status: 'processed'),
          'publish_request': {'id': 'pr1', 'status': 'stale', 'stale_reason': 'RUN_REFRESHED'},
        });
      await pumpPayroll(tester, const RunReviewScreen(runId: 'r1'), repo: repo, perms: _manage);
      expect(find.textContaining('The run was refreshed'), findsOneWidget);
      expect(find.byKey(const ValueKey('request-publish')), findsOneWidget);
    });

    testWidgets('second-person check off: says to publish directly and sends nothing', (tester) async {
      final repo = Fake5a()
        ..run0 = PayrollRun.fromJson(runJson(status: 'processed'))
        ..check = PublishCheck.fromJson(checkJson(makerChecker: false));
      await pumpPayroll(tester, const RunReviewScreen(runId: 'r1'), repo: repo, perms: _manage);
      await _tap(tester, 'request-publish');
      expect(find.textContaining('Publish directly'), findsOneWidget);
      expect(repo.created, isEmpty);
    });

    testWidgets('PUBLISH_REQUEST_EXISTS has a plain message', (tester) async {
      final repo = Fake5a()
        ..run0 = PayrollRun.fromJson(runJson(status: 'processed'))
        ..createError = const ApiException(code: 'PUBLISH_REQUEST_EXISTS', message: 'x', status: 409);
      await pumpPayroll(tester, const RunReviewScreen(runId: 'r1'), repo: repo, perms: _manage);
      await _tap(tester, 'request-publish');
      await _comment(tester, '');
      expect(find.textContaining('already waiting'), findsOneWidget);
      expect(find.textContaining('PUBLISH_REQUEST_EXISTS'), findsNothing);
    });

    testWidgets('a published run offers Payments and Delivery', (tester) async {
      final repo = Fake5a()..run0 = PayrollRun.fromJson(runJson(status: 'published'));
      await pumpPayroll(tester, const RunReviewScreen(runId: 'r1'), repo: repo, perms: _manage);
      expect(find.byKey(const ValueKey('run-payments')), findsOneWidget);
      await _tap(tester, 'run-delivery');
      expect(find.text('Delivery: October 2026'), findsOneWidget);
    });
  });

  group('approvals: publish requests and payment batches', () {
    testWidgets('a checker approves with the password; the version is sent', (tester) async {
      final repo = Fake5a()..requests = [_req()];
      await pumpPayroll(tester, const Scaffold(body: ApprovalsTab()), repo: repo, perms: _publish, selfId: 'checker');
      expect(find.text('Publish requests'), findsOneWidget);
      expect(find.textContaining('Payroll run of October 2026'), findsOneWidget);
      expect(find.textContaining('46 payslips, 2 on hold'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('queue-publish-approve-pr1')));
      await tester.pumpAndSettle();
      await _comment(tester, 'looks fine');
      await _password(tester, 'secret');
      expect(repo.approvals, [
        {'v': 1, 'pw': 'secret', 'comment': 'looks fine'},
      ]);
      expect(find.textContaining('Approved. The payslips were published'), findsOneWidget);
    });

    testWidgets('a wrong password asks again', (tester) async {
      final repo = Fake5a()
        ..requests = [_req()]
        ..pwRejects = 1;
      await pumpPayroll(tester, const Scaffold(body: ApprovalsTab()), repo: repo, perms: _publish, selfId: 'checker');
      await tester.tap(find.byKey(const ValueKey('queue-publish-approve-pr1')));
      await tester.pumpAndSettle();
      await _comment(tester, '');
      await _password(tester, 'bad');
      expect(find.text('That password is not correct.'), findsOneWidget);
      await _password(tester, 'good');
      expect(repo.approvals.map((a) => a['pw']), ['bad', 'good']);
    });

    testWidgets('reject needs a comment', (tester) async {
      final repo = Fake5a()..requests = [_req()];
      await pumpPayroll(tester, const Scaffold(body: ApprovalsTab()), repo: repo, perms: _publish, selfId: 'checker');
      await tester.tap(find.byKey(const ValueKey('queue-publish-reject-pr1')));
      await tester.pumpAndSettle();
      await _comment(tester, '');
      expect(repo.rejects, isEmpty);
      expect(find.byKey(const ValueKey('comment-field')), findsOneWidget);
      await _comment(tester, 'Hold is wrong');
      expect(repo.rejects, [
        {'v': 1, 'comment': 'Hold is wrong'},
      ]);
    });

    testWidgets('the requester sees the reason, not the buttons', (tester) async {
      final repo = Fake5a()..requests = [_req()];
      await pumpPayroll(tester, const Scaffold(body: ApprovalsTab()), repo: repo, perms: _publish, selfId: 'maker');
      expect(find.byKey(const ValueKey('queue-publish-approve-pr1')), findsNothing);
      expect(find.byKey(const ValueKey('queue-publish-reject-pr1')), findsNothing);
      expect(find.textContaining('You asked for this publish'), findsOneWidget);
    });

    testWidgets('a stale approve answer is explained and reloads', (tester) async {
      final repo = Fake5a()
        ..requests = [_req()]
        ..approveError = const ApiException(code: 'PUBLISH_STALE', message: 'x', status: 409);
      await pumpPayroll(tester, const Scaffold(body: ApprovalsTab()), repo: repo, perms: _publish, selfId: 'checker');
      await tester.tap(find.byKey(const ValueKey('queue-publish-approve-pr1')));
      await tester.pumpAndSettle();
      await _comment(tester, '');
      await _password(tester, 'pw');
      expect(find.textContaining('request is closed'), findsOneWidget);
    });

    testWidgets('payment batches waiting for approval are listed; the maker gets a reason', (tester) async {
      final repo = Fake5a()..batches = [_batch(status: 'submitted', submitter: 'sub')];
      await pumpPayroll(tester, const Scaffold(body: ApprovalsTab()), repo: repo, perms: _approve, selfId: 'maker');
      expect(find.text('Payment batches'), findsOneWidget);
      expect(find.text('PAY-2026-10-01'), findsOneWidget);
      expect(find.textContaining('₹70,600.00'), findsOneWidget);
      expect(find.byKey(const ValueKey('queue-batch-why-b1')), findsOneWidget);
    });

    testWidgets('empty queue', (tester) async {
      await pumpPayroll(tester, const Scaffold(body: ApprovalsTab()), repo: Fake5a(), perms: _all);
      expect(find.text('Nothing is waiting for your approval.'), findsOneWidget);
    });

    testWidgets('a publisher without payroll.approve still gets the Approvals tab', (tester) async {
      final repo = Fake5a()..requests = [_req()];
      await pumpPayroll(tester, const PayrollHomeScreen(), repo: repo, perms: _publish, selfId: 'checker');
      expect(find.byKey(const ValueKey('tab-approvals')), findsOneWidget);
      expect(find.byKey(const ValueKey('tab-payments')), findsOneWidget);
    });
  });

  group('publish request detail', () {
    testWidgets('submitted: snapshot, totals and the actions of a checker', (tester) async {
      final repo = Fake5a()
        ..request = _req()
        ..check = PublishCheck.fromJson(checkJson(canApprove: true));
      await pumpPayroll(tester, const PublishRequestDetailScreen(requestId: 'pr1'), repo: repo, perms: _all, selfId: 'checker');
      expect(find.textContaining('46 payslips, 2 on hold'), findsOneWidget);
      expect(find.text('₹16,90,000.00'), findsOneWidget);
      expect(find.byKey(const ValueKey('publish-approve')), findsOneWidget);
      expect(find.byKey(const ValueKey('publish-reject')), findsOneWidget);
      await _tap(tester, 'publish-approve');
      await _comment(tester, '');
      await _password(tester, 'pw');
      expect(repo.approvals.single['v'], 1);
    });

    testWidgets('a person who edited a payslip is told so and cannot decide', (tester) async {
      final repo = Fake5a()
        ..request = _req()
        ..check = PublishCheck.fromJson(checkJson(canApprove: true, me: ['EDITED_PAYSLIP']));
      await pumpPayroll(tester, const PublishRequestDetailScreen(requestId: 'pr1'), repo: repo, perms: _all, selfId: 'checker');
      expect(find.byKey(const ValueKey('publish-approve')), findsNothing);
      expect(find.textContaining('edited a payslip in it'), findsOneWidget);
    });

    testWidgets('the employee of a payslip request cannot decide', (tester) async {
      final repo = Fake5a()
        ..request = _req(scope: 'payslip')
        ..check = PublishCheck.fromJson(checkJson(canApprove: true));
      await pumpPayroll(tester, const PublishRequestDetailScreen(requestId: 'pr1'), repo: repo, perms: _all, selfId: 'emp');
      expect(find.byKey(const ValueKey('publish-approve')), findsNothing);
      expect(find.textContaining('your own payslip'), findsOneWidget);
    });

    testWidgets('a stale request explains itself and has no actions', (tester) async {
      final repo = Fake5a()..request = _req(status: 'stale', stale: 'EXPIRED');
      await pumpPayroll(tester, const PublishRequestDetailScreen(requestId: 'pr1'), repo: repo, perms: _all, selfId: 'checker');
      expect(find.byKey(const ValueKey('publish-stale-note')), findsOneWidget);
      expect(find.textContaining('expired'), findsOneWidget);
      expect(find.byKey(const ValueKey('publish-approve')), findsNothing);
      expect(find.byKey(const ValueKey('publish-withdraw')), findsNothing);
    });

    testWidgets('the maker can withdraw', (tester) async {
      final repo = Fake5a()..request = _req();
      await pumpPayroll(tester, const PublishRequestDetailScreen(requestId: 'pr1'), repo: repo, perms: _manage, selfId: 'maker');
      await _tap(tester, 'publish-withdraw');
      expect(repo.withdraws, [1]);
    });
  });

  group('payment batch detail', () {
    PaymentBatch withChange() => _batch(lines: [
          lineJson(changed: true, flags: ['RECENT_BANK_CHANGE']),
          lineJson(id: 'l2', seq: 2, user: 'u2'),
        ],);

    testWidgets('shows the reviewed and the current account (masked) and totals', (tester) async {
      final repo = Fake5a()..batch = withChange();
      await pumpPayroll(tester, const PaymentBatchScreen(batchId: 'b1'), repo: repo, perms: _manage, selfId: 'maker');
      expect(find.text('PAY-2026-10-01'), findsOneWidget);
      expect(find.text('₹70,600.00'), findsOneWidget);
      expect(find.textContaining('Reviewed account: XXXXXX1234 / HDFC0001234'), findsOneWidget);
      expect(find.textContaining('Current account: XXXXXX9876 / ICIC0000456'), findsOneWidget);
      expect(find.text('Bank details changed recently'), findsOneWidget);
      expect(find.textContaining('Debit account HDFC salary account (XXXXXX4321)'), findsOneWidget);
    });

    testWidgets('choosing the current account is staged, blocks submit, and is saved as account_sources', (tester) async {
      final repo = Fake5a()..batch = withChange();
      await pumpPayroll(tester, const PaymentBatchScreen(batchId: 'b1'), repo: repo, perms: _manage, selfId: 'maker');
      expect(tester.widget<ButtonStyleButton>(find.byKey(const ValueKey('batch-submit'))).onPressed, isNotNull);
      await tester.tap(find.byKey(const ValueKey('line-use-current-l1')));
      await tester.pumpAndSettle();
      expect(tester.widget<ButtonStyleButton>(find.byKey(const ValueKey('batch-submit'))).onPressed, isNull);
      await _tap(tester, 'batch-save-sources');
      expect(repo.edits, [
        {
          'expected_version': 4,
          'account_sources': [
            {'line_id': 'l1', 'source': 'current'},
          ],
        },
      ]);
    });

    testWidgets('a line already on the current account says so', (tester) async {
      final repo = Fake5a()
        ..batch = _batch(lines: [lineJson(changed: true, source: 'current')]);
      await pumpPayroll(tester, const PaymentBatchScreen(batchId: 'b1'), repo: repo, perms: _manage, selfId: 'maker');
      expect(find.text('The file pays the current account.'), findsOneWidget);
      expect(find.byKey(const ValueKey('line-use-current-l1')), findsNothing);
    });

    testWidgets('Refresh lines sends only the version; leave out and include send ids', (tester) async {
      final repo = Fake5a()
        ..batch = _batch(lines: [lineJson(), lineJson(id: 'l2', seq: 2, user: 'u2', status: 'excluded')]);
      await pumpPayroll(tester, const PaymentBatchScreen(batchId: 'b1'), repo: repo, perms: _manage, selfId: 'maker');
      await _tap(tester, 'batch-refresh');
      await _tap(tester, 'line-exclude-l1');
      await _tap(tester, 'line-include-l2');
      expect(repo.edits, [
        {'expected_version': 4},
        {
          'expected_version': 4,
          'exclude_line_ids': ['l1'],
        },
        {
          'expected_version': 4,
          'include_line_ids': ['l2'],
        },
      ]);
    });

    testWidgets('blockers of a draft are listed in plain words', (tester) async {
      final repo = Fake5a()
        ..batch = _batch(blockers: [
          {'user': userJson('u9', 'Latha'), 'payslip_id': 'p9', 'reason': 'INVALID_IFSC'},
        ],);
      await pumpPayroll(tester, const PaymentBatchScreen(batchId: 'b1'), repo: repo, perms: _manage, selfId: 'maker');
      expect(find.textContaining('Latha (EMP-u9): IFSC is not valid'), findsOneWidget);
    });

    testWidgets('submit sends the version; a stale answer is explained with line numbers', (tester) async {
      final repo = Fake5a()
        ..batch = _batch()
        ..batchError = const ApiException(
          code: 'BATCH_STALE',
          message: 'x',
          status: 409,
          details: [FieldError('lines[2]', 'PAYSLIP_SUPERSEDED')],
        );
      await pumpPayroll(tester, const PaymentBatchScreen(batchId: 'b1'), repo: repo, perms: _manage, selfId: 'maker');
      await _tap(tester, 'batch-submit');
      expect(repo.batchCalls, ['submit:4']);
      expect(find.textContaining('Line 2: the payslip was revised'), findsOneWidget);
    });

    testWidgets('the maker cannot approve; a checker approves with the password', (tester) async {
      final repo = Fake5a()..batch = _batch(status: 'submitted', submitter: 'sub');
      await pumpPayroll(tester, const PaymentBatchScreen(batchId: 'b1'), repo: repo, perms: _all, selfId: 'maker');
      expect(find.byKey(const ValueKey('batch-approve')), findsNothing);
      expect(find.byKey(const ValueKey('batch-cannot-decide')), findsOneWidget);
      await pumpPayroll(tester, const PaymentBatchScreen(batchId: 'b1'), repo: repo, perms: _approve, selfId: 'checker');
      await _tap(tester, 'batch-approve');
      await _comment(tester, '');
      await _password(tester, 'pw');
      expect(repo.batchApprovals, [
        {'v': 4, 'pw': 'pw', 'comment': ''},
      ]);
    });

    testWidgets('an employee paid by the batch cannot approve it', (tester) async {
      final repo = Fake5a()..batch = _batch(status: 'submitted', submitter: 'sub');
      await pumpPayroll(tester, const PaymentBatchScreen(batchId: 'b1'), repo: repo, perms: _approve, selfId: 'u2');
      expect(find.byKey(const ValueKey('batch-approve')), findsNothing);
      expect(find.textContaining('pays you'), findsOneWidget);
    });

    testWidgets('reject needs a comment', (tester) async {
      final repo = Fake5a()..batch = _batch(status: 'submitted', submitter: 'sub');
      await pumpPayroll(tester, const PaymentBatchScreen(batchId: 'b1'), repo: repo, perms: _approve, selfId: 'checker');
      await _tap(tester, 'batch-reject');
      await _comment(tester, '');
      expect(repo.batchCalls, isEmpty);
      await _comment(tester, 'Wrong account');
      expect(repo.batchCalls, ['reject:4:Wrong account']);
    });

    testWidgets('cancel needs a reason', (tester) async {
      final repo = Fake5a();
      await pumpPayroll(tester, const PaymentBatchScreen(batchId: 'b1'), repo: repo, perms: _manage, selfId: 'maker');
      await _tap(tester, 'batch-cancel');
      await _comment(tester, '');
      expect(repo.batchCalls, isEmpty);
      await _comment(tester, 'Duplicate batch');
      expect(repo.batchCalls, ['cancel:Duplicate batch']);
    });

    testWidgets('download: warning, password (retry), then the file goes to the share sheet', (tester) async {
      final delivery = FakeDelivery();
      final repo = Fake5a()
        ..batch = _batch(status: 'approved', submitter: 'sub')
        ..filePwRejects = 1;
      await pumpPayroll(
        tester,
        const PaymentBatchScreen(batchId: 'b1'),
        repo: repo,
        perms: _manage,
        selfId: 'maker',
        delivery: delivery,
      );
      await _tap(tester, 'batch-download');
      expect(find.textContaining('full account numbers'), findsOneWidget);
      await tester.tap(find.text('Download').last);
      await tester.pumpAndSettle();
      await _password(tester, 'bad');
      expect(find.text('That password is not correct.'), findsOneWidget);
      await _password(tester, 'good');
      expect(repo.files, ['bad', 'good']);
      expect(delivery.files.single.filename, 'bank-PAY-2026-10-01.csv');
    });

    testWidgets('download needs payroll.manage', (tester) async {
      final repo = Fake5a()..batch = _batch(status: 'approved', submitter: 'sub');
      await pumpPayroll(tester, const PaymentBatchScreen(batchId: 'b1'), repo: repo, perms: _approve, selfId: 'checker');
      expect(find.byKey(const ValueKey('batch-download')), findsNothing);
    });

    testWidgets('an exported batch shows the fingerprint and confirm; paid lines show a masked reference', (tester) async {
      final repo = Fake5a()
        ..batch = _batch(
          status: 'exported',
          submitter: 'sub',
          exportedAt: '2026-10-24T09:00:00Z',
          lines: [lineJson(status: 'exported'), lineJson(id: 'l2', seq: 2, user: 'u2', status: 'paid')],
        );
      await pumpPayroll(tester, const PaymentBatchScreen(batchId: 'b1'), repo: repo, perms: _manage, selfId: 'maker');
      expect(find.textContaining('File fingerprint aaaaaaaaaaaaaaaa'), findsOneWidget);
      expect(find.textContaining('Reference XXXX7788'), findsOneWidget);
      expect(find.byKey(const ValueKey('batch-confirm')), findsOneWidget);
      expect(find.byKey(const ValueKey('batch-cancel')), findsNothing);
    });
  });

  group('confirm payments', () {
    PaymentBatch exported() => _batch(
          status: 'exported',
          submitter: 'sub',
          exportedAt: '2026-10-24T09:00:00Z',
          lines: [lineJson(status: 'exported'), lineJson(id: 'l2', seq: 2, user: 'u2', status: 'exported')],
        );

    testWidgets('all paid: paid_on, shared reference and the password', (tester) async {
      final repo = Fake5a()..batch = exported();
      await pumpPayroll(tester, const PaymentBatchScreen(batchId: 'b1'), repo: repo, perms: _manage, selfId: 'maker');
      await _tap(tester, 'batch-confirm');
      await tester.enterText(find.byKey(const ValueKey('confirm-batch-ref')), 'BATCH-1');
      await _tapBusy(tester, 'confirm-submit');
      await _password(tester, 'pw');
      expect(repo.confirms, [
        {
          'paid_on': '2026-10-25',
          'lines': null,
          'all_paid': true,
          'reference': 'BATCH-1',
          'current_password': 'pw',
        },
      ]);
    });

    testWidgets('line by line: needs a choice, a valid reference, and sends paid / failed', (tester) async {
      final repo = Fake5a()..batch = exported();
      await pumpPayroll(tester, const PaymentBatchScreen(batchId: 'b1'), repo: repo, perms: _manage, selfId: 'maker');
      await _tap(tester, 'batch-confirm');
      await tester.tap(find.text('Line by line'));
      await tester.pumpAndSettle();
      await _tap(tester, 'confirm-submit');
      expect(find.text('Mark at least one line as paid or failed.'), findsOneWidget);
      await tester.tap(find.descendant(
        of: find.byKey(const ValueKey('confirm-line-l1')),
        matching: find.text('Paid'),
      ),);
      await tester.tap(find.descendant(
        of: find.byKey(const ValueKey('confirm-line-l2')),
        matching: find.text('Failed'),
      ),);
      await tester.pumpAndSettle();
      await tester.enterText(find.byKey(const ValueKey('confirm-ref-l1')), 'bad;ref');
      await _tap(tester, 'confirm-submit');
      expect(find.text('Letters, digits, space and . _ / - only'), findsOneWidget);
      expect(repo.confirms, isEmpty);
      await tester.enterText(find.byKey(const ValueKey('confirm-ref-l1')), 'UTR1');
      await _tapBusy(tester, 'confirm-submit');
      await _password(tester, 'pw');
      final body = repo.confirms.single;
      expect(body['all_paid'], false);
      expect(body['lines'], [
        {'line_id': 'l1', 'status': 'paid', 'reference': 'UTR1'},
        {'line_id': 'l2', 'status': 'failed'},
      ]);
    });
  });

  group('payment batches list and creation', () {
    testWidgets('empty state and the list with a status filter', (tester) async {
      final repo = Fake5a();
      await pumpPayroll(tester, const PaymentBatchesScreen(runId: 'r1'), repo: repo, perms: _manage);
      expect(find.textContaining('No payment batches yet'), findsOneWidget);
      repo.batches = [_batch(status: 'approved', submitter: 'sub')];
      await tester.tap(find.byKey(const ValueKey('batch-filter-approved')));
      await tester.pumpAndSettle();
      expect(find.text('PAY-2026-10-01'), findsOneWidget);
      expect(find.text('Approved'), findsWidgets);
    });

    testWidgets('create: validates nothing to pay with the blockers in words', (tester) async {
      final repo = Fake5a()
        ..accounts = [_account()]
        ..createBatchError = const ApiException(
          code: 'NOTHING_TO_PAY',
          message: 'x',
          status: 422,
          details: [FieldError('emp-1', 'NO_BANK_DETAILS'), FieldError('emp-2', 'HELD')],
        );
      await pumpPayroll(tester, const CreateBatchScreen(runId: 'r1', period: '2026-10'), repo: repo, perms: _manage);
      await _tap(tester, 'batch-create');
      expect(repo.createdBatches.single['value_date'], '2026-10-25');
      expect(repo.createdBatches.single['debit_account_id'], isNull);
      expect(find.textContaining('1 x No bank details'), findsOneWidget);
      expect(find.textContaining('emp-1'), findsNothing);
    });

    testWidgets('create opens the new draft batch', (tester) async {
      final repo = Fake5a()..accounts = [_account()];
      await pumpPayroll(tester, const CreateBatchScreen(runId: 'r1'), repo: repo, perms: _manage, selfId: 'maker');
      await _tap(tester, 'batch-create');
      expect(find.text('Payment batch'), findsOneWidget);
      expect(find.text('PAY-2026-10-01'), findsOneWidget);
    });
  });

  group('bank accounts', () {
    testWidgets('lists masked accounts; deactivate asks, and IN_USE is explained', (tester) async {
      final repo = Fake5a()
        ..accounts = [_account(), _account(id: 'ba2', isDefault: false)]
        ..accountError = const ApiException(code: 'IN_USE', message: 'x', status: 409);
      await pumpPayroll(tester, const Scaffold(body: BankAccountsSection()), repo: repo, perms: _manage);
      expect(find.textContaining('XXXXXX4321'), findsNWidgets(2));
      expect(find.text('Default'), findsOneWidget);
      await _tap(tester, 'bank-deactivate-ba2');
      await tester.tap(find.text('Deactivate').last);
      await tester.pumpAndSettle();
      expect(repo.accountUpdates.single['active'], false);
      expect(find.textContaining('still a draft, waiting or approved'), findsOneWidget);
    });

    testWidgets('making an account the default asks for the password', (tester) async {
      final repo = Fake5a()..accounts = [_account(), _account(id: 'ba2', isDefault: false)];
      await pumpPayroll(tester, const Scaffold(body: BankAccountsSection()), repo: repo, perms: _manage);
      await _tap(tester, 'bank-default-ba2');
      await _password(tester, 'secret');
      expect(repo.accountUpdates.single['is_default'], true);
      expect(repo.accountPasswords.single, 'secret');
    });

    testWidgets('a viewer sees accounts but no actions', (tester) async {
      final repo = Fake5a()..accounts = [_account()];
      await pumpPayroll(tester, const Scaffold(body: BankAccountsSection()), repo: repo, perms: const ['payroll.view_all']);
      expect(find.byKey(const ValueKey('add-bank-account')), findsNothing);
      expect(find.byKey(const ValueKey('bank-deactivate-ba1')), findsNothing);
    });

    testWidgets('empty state', (tester) async {
      await pumpPayroll(tester, const Scaffold(body: BankAccountsSection()), repo: Fake5a(), perms: _manage);
      expect(find.textContaining('No company bank accounts yet'), findsOneWidget);
    });

    testWidgets('add: validates, then asks for the password and sends the typed number once', (tester) async {
      final repo = Fake5a()..accounts = [_account()];
      await pumpPayroll(tester, const AddBankAccountScreen(), repo: repo, perms: _manage);
      await _tap(tester, 'bank-save');
      expect(find.text('Required'), findsWidgets);
      await tester.enterText(find.byKey(const ValueKey('bank-label')), 'Main');
      await tester.enterText(find.byKey(const ValueKey('bank-name')), 'HDFC Bank');
      await tester.enterText(find.byKey(const ValueKey('bank-account-number')), '12345');
      await tester.enterText(find.byKey(const ValueKey('bank-ifsc')), 'HDFC1');
      await _tap(tester, 'bank-save');
      expect(find.text('Enter 9 to 18 digits'), findsOneWidget);
      expect(find.textContaining('IFSC is 11 characters'), findsOneWidget);
      expect(find.text('Confirm your password'), findsNothing);
      await tester.enterText(find.byKey(const ValueKey('bank-account-number')), '123456789012');
      await tester.enterText(find.byKey(const ValueKey('bank-ifsc')), 'hdfc0001234');
      await _tapBusy(tester, 'bank-save');
      await _password(tester, 'pw');
      expect(repo.accountCreates.single['account'], '123456789012');
      expect(repo.accountCreates.single['ifsc'], 'HDFC0001234');
      expect(repo.accountCreates.single['current_password'], 'pw');
    });

    testWidgets('the account number field hides what is typed', (tester) async {
      await pumpPayroll(tester, const AddBankAccountScreen(), repo: Fake5a(), perms: _manage);
      final field = tester.widget<TextField>(find.descendant(
        of: find.byKey(const ValueKey('bank-account-number')),
        matching: find.byType(TextField),
      ),);
      expect(field.obscureText, isTrue);
      expect(field.enableSuggestions, isFalse);
    });
  });

  group('delivery report', () {
    testWidgets('counts, reminder rules and the skip summary', (tester) async {
      final repo = Fake5a();
      await pumpPayroll(tester, const DeliveryReportScreen(runId: 'r1', period: '2026-10'), repo: repo, perms: _manage);
      expect(find.textContaining('Opened 1'), findsWidgets);
      expect(find.textContaining('No phone 1'), findsOneWidget);
      // reminded 3 times and no phone: no bell
      expect(find.byKey(const ValueKey('remind-u1')), findsOneWidget);
      expect(find.byKey(const ValueKey('remind-u2')), findsNothing);
      expect(find.byKey(const ValueKey('remind-u3')), findsNothing);
      expect(find.textContaining('Reminded 3 of 3'), findsOneWidget);
      await _tap(tester, 'remind-u1');
      expect(repo.reminds.single, ['u1']);
      expect(find.textContaining('1 reminder sent. 1 skipped: 1 already opened it.'), findsOneWidget);
    });

    testWidgets('remind everyone asks first and sends null user_ids', (tester) async {
      final repo = Fake5a();
      await pumpPayroll(tester, const DeliveryReportScreen(runId: 'r1'), repo: repo, perms: _manage);
      await _tap(tester, 'remind-all');
      expect(find.textContaining('at most 3 times'), findsOneWidget);
      await tester.tap(find.text('Send reminders'));
      await tester.pumpAndSettle();
      expect(repo.reminds, [null]);
    });

    testWidgets('a viewer cannot remind', (tester) async {
      await pumpPayroll(tester, const DeliveryReportScreen(runId: 'r1'), repo: Fake5a(), perms: const ['payroll.view_all']);
      expect(find.byKey(const ValueKey('remind-all')), findsNothing);
      expect(find.byKey(const ValueKey('remind-u1')), findsNothing);
    });
  });

  group('payments tab', () {
    testWidgets('switches between batches, accounts and delivery', (tester) async {
      final repo = Fake5a()
        ..batches = [_batch(status: 'approved', submitter: 'sub')]
        ..accounts = [_account()]
        ..runList = [
          PayrollRunItem.fromJson({
            'id': 'r1',
            'period': '2026-10',
            'status': 'published',
            'employees': 2,
            'net_pay_total': '1.00',
          }),
        ];
      await pumpPayroll(tester, const Scaffold(body: PaymentsTab()), repo: repo, perms: _manage);
      expect(find.text('PAY-2026-10-01'), findsOneWidget);
      await tester.tap(find.text('Accounts'));
      await tester.pumpAndSettle();
      expect(find.text('HDFC salary account'), findsOneWidget);
      await tester.tap(find.text('Delivery'));
      await tester.pumpAndSettle();
      expect(find.text('October 2026'), findsOneWidget);
    });

    testWidgets('errors show a retry', (tester) async {
      final repo = _FailingBatches();
      await pumpPayroll(tester, const Scaffold(body: PaymentsTab()), repo: repo, perms: _manage);
      expect(find.text('Try again'), findsOneWidget);
    });
  });

  group('small phones', () {
    const small = Size(360, 700);

    testWidgets('batch detail, request detail and the payments tab fit 360 px', (tester) async {
      final repo = Fake5a()
        ..batch = _batch(lines: [
          lineJson(changed: true, flags: ['RECENT_BANK_CHANGE', 'NO_PAN', 'REVISION_DELTA']),
        ],)
        ..request = _req()
        ..check = PublishCheck.fromJson(checkJson(canApprove: true))
        ..batches = [_batch(status: 'submitted', submitter: 'sub')]
        ..accounts = [_account()];
      await pumpPayroll(tester, const PaymentBatchScreen(batchId: 'b1'), repo: repo, perms: _all, selfId: 'x', size: small);
      expect(tester.takeException(), isNull);
      await pumpPayroll(
        tester,
        const PublishRequestDetailScreen(requestId: 'pr1'),
        repo: repo,
        perms: _all,
        selfId: 'checker',
        size: small,
      );
      expect(tester.takeException(), isNull);
      await pumpPayroll(tester, const Scaffold(body: PaymentsTab()), repo: repo, perms: _all, size: small);
      expect(tester.takeException(), isNull);
      await pumpPayroll(
        tester,
        const DeliveryReportScreen(runId: 'r1', period: '2026-10'),
        repo: repo,
        perms: _all,
        size: small,
      );
      expect(tester.takeException(), isNull);
    });
  });

  group('settings and dashboard', () {
    Map<String, Object?> s5() => {
          'payroll.publish_request_ttl_hours': 72,
          'payroll.bank_file_format': 'generic_neft_csv',
          'payroll.bank_payment_type': 'NEFT',
          'payroll.bank_change_alert_days': 30,
        };

    testWidgets('the removed bank file switch is not on the settings tab', (tester) async {
      final repo = Fake5a()..settingsMap = s5();
      await pumpPayroll(tester, const Scaffold(body: PayrollSettingsTab()), repo: repo, perms: _manage);
      expect(find.text('A different person must approve the bank payment file'), findsNothing);
      expect(find.textContaining('bank payment file'), findsNothing);
    });

    testWidgets('the bank change window is locked without role.assign', (tester) async {
      final repo = Fake5a()..settingsMap = s5();
      await pumpPayroll(tester, const Scaffold(body: PayrollSettingsTab()), repo: repo, perms: _manage);
      final field = find.widgetWithText(TextFormField, 'Flag bank detail changes made in the last');
      expect(tester.widget<TextField>(find.descendant(of: field, matching: find.byType(TextField))).enabled, isFalse);
      expect(find.text('Only an administrator can change this.'), findsOneWidget);
    });

    testWidgets('an administrator changes the bank change window with the password; below 7 is refused', (tester) async {
      final repo = Fake5a()..settingsMap = s5();
      await pumpPayroll(
        tester,
        const Scaffold(body: PayrollSettingsTab()),
        repo: repo,
        perms: [..._manage, 'role.assign'],
      );
      final field = find.widgetWithText(TextFormField, 'Flag bank detail changes made in the last');
      await tester.enterText(field, '3');
      await tester.tap(find.byKey(const ValueKey('save-settings')));
      await tester.pumpAndSettle();
      expect(find.text('At least 7'), findsOneWidget);
      expect(repo.savedSettings, isEmpty);
      await tester.enterText(field, '14');
      await tester.tap(find.byKey(const ValueKey('save-settings')));
      await tester.pump(const Duration(milliseconds: 600));
      await tester.pump(const Duration(seconds: 1));
      await _password(tester, 'secret');
      expect(repo.savedSettings.single, {'payroll.bank_change_alert_days': 14});
      expect(repo.settingsPasswords.single, 'secret');
    });

    testWidgets('the other new settings need no password and are range checked', (tester) async {
      final repo = Fake5a()..settingsMap = s5();
      await pumpPayroll(tester, const Scaffold(body: PayrollSettingsTab()), repo: repo, perms: _manage);
      final ttl = find.widgetWithText(TextFormField, 'A publish request expires after');
      await tester.enterText(ttl, '400');
      await tester.tap(find.byKey(const ValueKey('save-settings')));
      await tester.pumpAndSettle();
      expect(find.text('At most 336'), findsOneWidget);
      expect(repo.savedSettings, isEmpty);
      await tester.enterText(ttl, '48');
      await tester.tap(find.byKey(const ValueKey('save-settings')));
      await tester.pumpAndSettle();
      expect(repo.savedSettings.single, {'payroll.publish_request_ttl_hours': 48});
      expect(repo.settingsPasswords, everyElement(isNull));
    });

    testWidgets('dashboard alerts for publish and payments', (tester) async {
      final repo = Fake5a();
      repo.dashboardJson = {
        ...repo.dashboardJson,
        'calendar': {
          'process_by': '2026-10-25',
          'pay_date': '2026-10-31',
          'alerts': ['PUBLISH_PENDING_APPROVAL', 'PAYMENT_NOT_EXPORTED', 'PAYMENT_FAILED_LINES', 'PAYMENT_UNDERPAID'],
        },
      };
      await pumpPayroll(tester, const Scaffold(body: PayrollDashboardTab()), repo: repo, perms: _manage);
      expect(find.textContaining('publish request is waiting'), findsOneWidget);
      expect(find.textContaining('has not been sent to the bank'), findsOneWidget);
      expect(find.textContaining('bank payments failed'), findsOneWidget);
      expect(find.textContaining('less than their net pay'), findsOneWidget);
    });
  });
}

class _FailingBatches extends Fake5a {
  @override
  Future<Paged<PaymentBatch>> paymentBatches(PaymentBatchQuery q, {int page = 1}) async =>
      throw const ApiException(code: 'NETWORK_ERROR', message: 'offline');
}
