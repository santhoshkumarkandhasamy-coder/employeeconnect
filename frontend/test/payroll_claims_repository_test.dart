import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/payroll/data/payroll_repository.dart';
import 'package:employee_connect/features/payroll/domain/claims_models.dart';
import 'package:employee_connect/features/payroll/domain/payroll_models.dart' show payrollErrorMessage;
import 'package:flutter_test/flutter_test.dart';

/// Repository paths, bodies and parsing for contract sections 17.6 and 17.8 (work package
/// 3b) against HAND-WRITTEN fixtures (`payroll_phase3b_contract_samples.json`),
/// not responses captured from a backend. The Dio transport is stubbed.
class _Call {
  _Call(this.method, this.path, this.body, this.query);
  final String method;
  final String path;
  final Object? body;
  final Map<String, dynamic> query;

  Map<String, dynamic> get json => body! as Map<String, dynamic>;
  FormData get form => body! as FormData;
  Map<String, String> get fields => {for (final e in form.fields) e.key: e.value};
}

class _Reply {
  _Reply(this.status, this.json);
  final int status;
  final Object? json;
}

class _Adapter implements HttpClientAdapter {
  _Adapter(this.reply);
  final _Reply Function(_Call call) reply;
  final List<_Call> calls = [];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    Object? body = options.data;
    if (body is String && body.isNotEmpty) body = jsonDecode(body);
    final c = _Call(options.method, options.path, body, options.queryParameters);
    calls.add(c);
    final r = reply(c);
    return ResponseBody.fromString(
      r.json == null ? '' : jsonEncode(r.json),
      r.status,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}

(PayrollRepository, _Adapter) _repo(_Reply Function(_Call) reply) {
  final a = _Adapter(reply);
  final dio = Dio(BaseOptions(baseUrl: 'http://x/api'))..httpClientAdapter = a;
  return (PayrollRepository(dio), a);
}

late Map<String, dynamic> samples;

Future<ApiException> _thrown(Future<Object?> Function() call) async {
  try {
    await call();
  } on ApiException catch (e) {
    return e;
  }
  fail('expected an ApiException');
}

void main() {
  setUpAll(() {
    samples = jsonDecode(File('test/fixtures/payroll_phase3b_contract_samples.json').readAsStringSync())
        as Map<String, dynamic>;
  });

  Map<String, dynamic> pageOf(List<Object?> items) =>
      {'items': items, 'page': 1, 'page_size': 20, 'total': items.length};

  group('claims: employee', () {
    test('categories', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['categories']));
      final c = await repo.claimCategories();
      expect(a.calls.single.method, 'GET');
      expect(a.calls.single.path, '/reimbursements/categories');
      expect(c.items.first.maxAmount, '5000.00');
    });

    test('bill upload is multipart with purpose reimbursement_bill and the right content type', () async {
      final (repo, a) = _repo((c) => _Reply(201, {'id': 'file-9'}));
      final id = await repo.uploadBill([1, 2, 3, 4], 'taxi.pdf');
      final c = a.calls.single;
      expect(c.method, 'POST');
      expect(c.path, '/files');
      expect(c.fields, {'purpose': 'reimbursement_bill'});
      expect(c.form.files.single.key, 'file');
      expect(c.form.files.single.value.filename, 'taxi.pdf');
      expect(c.form.files.single.value.length, 4);
      expect(c.form.files.single.value.contentType.toString(), 'application/pdf');
      expect(id, 'file-9');
    });

    test('submit posts exactly the contract keys; amount stays a string; bill id may be null', () async {
      final (repo, a) = _repo((c) => _Reply(201, samples['claim']));
      final r = await repo.submitClaim(
        category: 'travel',
        expenseDate: '2026-10-28',
        amount: '1240.00',
        description: 'Cab to client site',
        billFileId: 'file-9',
      );
      expect(a.calls.single.method, 'POST');
      expect(a.calls.single.path, '/reimbursements');
      expect(a.calls.single.json, {
        'category': 'travel',
        'expense_date': '2026-10-28',
        'amount': '1240.00',
        'description': 'Cab to client site',
        'bill_file_id': 'file-9',
      });
      expect(r.status, 'submitted');
      await repo.submitClaim(category: 'phone', expenseDate: '2026-10-28', amount: '10.00', description: 'Recharge');
      expect(a.calls.last.json['bill_file_id'], isNull);
      expect(a.calls.last.json.containsKey('bill_file_id'), isTrue);
    });

    test('list sends only the filters that are set; detail and cancel paths', () async {
      final (repo, a) = _repo(
        (c) => _Reply(
          200,
          c.method == 'GET' && c.path == '/reimbursements'
              ? pageOf([samples['claim'], samples['claim_approved']])
              : samples['claim'],
        ),
      );
      final p = await repo.myClaims(const ClaimQuery(status: 'approved', year: 2026), page: 2);
      expect(a.calls.single.query, {'status': 'approved', 'year': 2026, 'page': 2, 'page_size': 20});
      expect(p.items, hasLength(2));
      await repo.myClaims(const ClaimQuery());
      expect(a.calls.last.query, {'page': 1, 'page_size': 20});
      await repo.myClaim('c1');
      expect(a.calls.last.path, '/reimbursements/c1');
      await repo.cancelClaim('c1');
      expect(a.calls.last.method, 'POST');
      expect(a.calls.last.path, '/reimbursements/c1/cancel');
    });

    test('a bill is fetched by file id only, through the app client', () async {
      final (repo, a) = _repo((c) => _Reply(200, 'xyz'));
      final bytes = await repo.billBytes('https://evil.example/api/files/f1111111-1111-1111-1111-111111111111');
      expect(a.calls.single.method, 'GET');
      expect(a.calls.single.path, '/files/f1111111-1111-1111-1111-111111111111');
      expect(bytes, isNotEmpty);
      final e = await _thrown(() => repo.billBytes('not-a-file-link'));
      expect(e.code, 'NOT_FOUND');
      expect(a.calls, hasLength(1));
    });

    test('submit errors keep their codes and details', () async {
      final (repo, _) = _repo((c) => _Reply(422, samples['errors']['AMOUNT_ABOVE_LIMIT']));
      final e = await _thrown(
        () => repo.submitClaim(category: 'travel', expenseDate: '2026-10-28', amount: '9000.00', description: 'Cab'),
      );
      expect(e.code, 'AMOUNT_ABOVE_LIMIT');
      expect(e.details.first.message, '5000.00');
      expect(payrollErrorMessage(e), contains('₹5,000.00'));
      final (repo2, _) = _repo((c) => _Reply(409, samples['errors']['DUPLICATE_BILL']));
      final e2 = await _thrown(
        () => repo2.submitClaim(category: 'travel', expenseDate: '2026-10-28', amount: '9.00', description: 'Cab'),
      );
      expect(e2.code, 'DUPLICATE_BILL');
    });
  });

  group('claims: manager and HR', () {
    test('list sends stage and only the set filters', () async {
      final (repo, a) = _repo((c) => _Reply(200, pageOf([samples['admin_claim_verified']])));
      await repo.adminClaims(const AdminClaimQuery(stage: 'manager'));
      expect(a.calls.single.path, '/admin/reimbursements');
      expect(a.calls.single.query, {'stage': 'manager', 'page': 1, 'page_size': 20});
      await repo.adminClaims(
        const AdminClaimQuery(
          stage: 'all',
          status: 'paid',
          period: '2026-11',
          category: 'travel',
          employeeId: ' emp001 ',
        ),
        page: 3,
      );
      expect(a.calls.last.query, {
        'stage': 'all',
        'status': 'paid',
        'period': '2026-11',
        'category': 'travel',
        'employee_id': 'emp001',
        'page': 3,
        'page_size': 20,
      });
    });

    test('verify sends the optional comment only when given', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['admin_claim_verified']));
      await repo.verifyClaim('c4');
      expect(a.calls.single.method, 'PUT');
      expect(a.calls.single.path, '/admin/reimbursements/c4/verify');
      expect(a.calls.single.json, <String, dynamic>{});
      await repo.verifyClaim('c4', comment: ' Looks fine ');
      expect(a.calls.last.json, {'comment': 'Looks fine'});
    });

    test('approve sends amount, period, optional comment and expected_version', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['claim_approved']));
      await repo.approveClaim('c4', approvedAmount: '700.00', period: '2026-11', expectedVersion: 2);
      expect(a.calls.single.method, 'PUT');
      expect(a.calls.single.path, '/admin/reimbursements/c4/approve');
      expect(a.calls.single.json, {'approved_amount': '700.00', 'period': '2026-11', 'expected_version': 2});
      await repo.approveClaim('c4', approvedAmount: '700.00', period: '2026-11', expectedVersion: 2, comment: 'ok');
      expect(a.calls.last.json['comment'], 'ok');
    });

    test('reject needs the comment; payout sends period and expected_version', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['claim_approved']));
      await repo.rejectClaim('c4', comment: ' Personal ');
      expect(a.calls.single.path, '/admin/reimbursements/c4/reject');
      expect(a.calls.single.json, {'comment': 'Personal'});
      await repo.payoutClaim('c4', period: '2026-12', expectedVersion: 3);
      expect(a.calls.last.method, 'PUT');
      expect(a.calls.last.path, '/admin/reimbursements/c4/payout');
      expect(a.calls.last.json, {'period': '2026-12', 'expected_version': 3});
    });

    test('decision errors keep their codes', () async {
      final (repo, _) = _repo((c) => _Reply(403, samples['errors']['SECOND_PERSON_REQUIRED']));
      final e = await _thrown(
        () => repo.approveClaim('c4', approvedAmount: '1.00', period: '2026-11', expectedVersion: 1),
      );
      expect(e.code, 'SECOND_PERSON_REQUIRED');
    });
  });

  group('overtime', () {
    test('compute posts with the period in the query', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['overtime_compute']));
      final r = await repo.computeOvertime('2026-10');
      expect(a.calls.single.method, 'POST');
      expect(a.calls.single.path, '/admin/payroll/overtime/compute');
      expect(a.calls.single.query, {'period': '2026-10'});
      expect(r.created, 34);
    });

    test('list sends the filters; the summary reads one row only', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['overtime_page']));
      final p = await repo.overtimeDays(const OvertimeQuery(period: '2026-10', status: 'computed', userId: 'u1'));
      expect(a.calls.single.path, '/admin/payroll/overtime');
      expect(
        a.calls.single.query,
        {'period': '2026-10', 'status': 'computed', 'user_id': 'u1', 'page': 1, 'page_size': 50},
      );
      expect(p.items, hasLength(3));
      final s = await repo.overtimeSummary(const OvertimeQuery(period: '2026-10'));
      expect(a.calls.last.query, {'period': '2026-10', 'page': 1, 'page_size': 1});
      expect(s, hasLength(2));
    });

    test('approve sends status, minutes, comment and expected_version', () async {
      final (repo, a) = _repo((c) => _Reply(200, (samples['overtime_page']['items'] as List).first));
      await repo.approveOvertime('o1', approvedMinutes: 90, expectedVersion: 1);
      expect(a.calls.single.method, 'PUT');
      expect(a.calls.single.path, '/admin/payroll/overtime/o1');
      expect(a.calls.single.json, {
        'status': 'approved',
        'approved_minutes': 90,
        'comment': null,
        'expected_version': 1,
      });
    });

    test('reject sends the comment', () async {
      final (repo, a) = _repo((c) => _Reply(200, (samples['overtime_page']['items'] as List).first));
      await repo.rejectOvertime('o1', comment: ' Not worked ', expectedVersion: 2);
      expect(a.calls.single.json, {'status': 'rejected', 'comment': 'Not worked', 'expected_version': 2});
    });

    test('bulk sends ids, action and the comment only when given', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['overtime_bulk']));
      final r = await repo.bulkOvertime(['o1', 'o2'], action: 'approve');
      expect(a.calls.single.method, 'POST');
      expect(a.calls.single.path, '/admin/payroll/overtime/bulk');
      expect(a.calls.single.json, {
        'ids': ['o1', 'o2'],
        'action': 'approve',
      });
      expect(r.skipped, hasLength(2));
      await repo.bulkOvertime(['o1'], action: 'reject', comment: 'No attendance proof');
      expect(a.calls.last.json['comment'], 'No attendance proof');
    });

    test('OVERTIME_DISABLED keeps its code', () async {
      final (repo, _) = _repo((c) => _Reply(409, samples['errors']['OVERTIME_DISABLED']));
      final e = await _thrown(() => repo.computeOvertime('2026-10'));
      expect(e.code, 'OVERTIME_DISABLED');
      expect(payrollErrorMessage(e), contains('switched off'));
    });

    test('my overtime asks for the period and parses enabled false', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['me_overtime_disabled']));
      final o = await repo.myOvertime('2026-10');
      expect(a.calls.single.path, '/me/overtime');
      expect(a.calls.single.query, {'period': '2026-10'});
      expect(o.visible, isFalse);
    });
  });

  group('details: overtime_eligible', () {
    test('is sent alone, with no password key', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['details_eligible']));
      final d = await repo.saveOvertimeEligible('u1', eligible: true);
      expect(a.calls.single.method, 'PUT');
      expect(a.calls.single.path, '/admin/payroll/employees/u1/details');
      expect(a.calls.single.json, {'overtime_eligible': true});
      expect(d.overtimeEligible, isTrue);
      await repo.saveOvertimeEligible('u1', eligible: false);
      expect(a.calls.last.json, {'overtime_eligible': false});
    });
  });
}
