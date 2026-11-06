import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/payroll/data/payroll_repository.dart';
import 'package:employee_connect/features/payroll/domain/phase5a_models.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/payroll_fakes.dart' show runJson;
import 'payroll_phase5a_logic_test.dart' show batchJson, checkJson, requestJson;

/// Paths, bodies and parsing of the 5a calls (api-contract.md 19.2 to 19.5)
/// against a stubbed Dio transport.
class _Call {
  _Call(this.method, this.path, this.body, this.query);
  final String method;
  final String path;
  final Object? body;
  final Map<String, dynamic> query;
}

class _Adapter implements HttpClientAdapter {
  _Adapter(this.reply);
  final (int, Object?, Map<String, List<String>>) Function(_Call call) reply;
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
    final (status, json, headers) = reply(c);
    final text = json is String ? json : (json == null ? '' : jsonEncode(json));
    return ResponseBody.fromString(
      text,
      status,
      headers: {
        if (json is! String) Headers.contentTypeHeader: [Headers.jsonContentType],
        ...headers,
      },
    );
  }

  @override
  void close({bool force = false}) {}
}

(PayrollRepository, _Adapter) _repo(Object? Function(_Call) reply, {int status = 200}) {
  final a = _Adapter((c) => (status, reply(c), <String, List<String>>{}));
  final dio = Dio(BaseOptions(baseUrl: 'http://x/api'))..httpClientAdapter = a;
  return (PayrollRepository(dio), a);
}

void main() {
  test('publish check and create request (comment optional, blank -> null)', () async {
    final (repo, a) = _repo((c) => c.method == 'GET' ? checkJson() : requestJson());
    final check = await repo.publishCheckRun('r1');
    expect(check.makerChecker, isTrue);
    expect(a.calls.last.path, '/admin/payroll/runs/r1/publish-check');
    await repo.publishCheckPayslip('p1');
    expect(a.calls.last.path, '/admin/payroll/payslips/p1/publish-check');
    final r = await repo.createRunPublishRequest('r1', comment: '  ');
    expect(r.id, 'pr1');
    expect(a.calls.last.method, 'POST');
    expect(a.calls.last.path, '/admin/payroll/runs/r1/publish-requests');
    expect(a.calls.last.body, {'comment': null});
    await repo.createPayslipPublishRequest('p1', comment: ' please ');
    expect(a.calls.last.path, '/admin/payroll/payslips/p1/publish-requests');
    expect(a.calls.last.body, {'comment': 'please'});
  });

  test('list, withdraw, approve (step-up) and reject (comment)', () async {
    final (repo, a) = _repo((c) {
      if (c.path.endsWith('/approve')) {
        return {'request': requestJson(status: 'approved'), 'run': runJson(status: 'published')};
      }
      if (c.path == '/admin/payroll/publish-requests') {
        return {'items': [requestJson(totals: false)], 'page': 1, 'page_size': 20, 'total': 1};
      }
      return requestJson();
    });
    final page = await repo.publishRequests(const PublishRequestQuery(status: 'submitted', runId: 'r1'));
    expect(page.items.single.snapshot.totals, isNull);
    expect(a.calls.last.query, {'status': 'submitted', 'run_id': 'r1', 'page': 1, 'page_size': 20});
    await repo.publishRequest('pr1');
    expect(a.calls.last.path, '/admin/payroll/publish-requests/pr1');
    await repo.withdrawPublishRequest('pr1', expectedVersion: 4);
    expect(a.calls.last.path, '/admin/payroll/publish-requests/pr1/withdraw');
    expect(a.calls.last.body, {'expected_version': 4});
    final res = await repo.approvePublishRequest('pr1', expectedVersion: 2, currentPassword: 'pw');
    expect(res.run!.isPublished, isTrue);
    expect(a.calls.last.body, {'expected_version': 2, 'comment': null, 'current_password': 'pw'});
    await repo.rejectPublishRequest('pr1', expectedVersion: 2, comment: ' not now ');
    expect(a.calls.last.path, '/admin/payroll/publish-requests/pr1/reject');
    expect(a.calls.last.body, {'expected_version': 2, 'comment': 'not now'});
  });

  test('409 PUBLISH_REQUEST_REQUIRED on direct publish is an ApiException with its code', () async {
    final (repo, _) = _repo(
      (c) => {
        'error': {'code': 'PUBLISH_REQUEST_REQUIRED', 'message': 'x', 'details': <Object>[], 'request_id': 'q'},
      },
      status: 409,
    );
    await expectLater(
      repo.publishRun('r1', currentPassword: 'pw'),
      throwsA(isA<ApiException>().having((e) => e.code, 'code', 'PUBLISH_REQUEST_REQUIRED')),
    );
  });

  test('delivery report and reminders', () async {
    final (repo, a) = _repo((c) {
      if (c.path.endsWith('/remind')) return {'sent': 2, 'skipped': <Object>[]};
      return {
        'counts': {'published': 1, 'pushed': 1, 'viewed': 0, 'downloaded': 0, 'no_device': 0},
        'items': <Object>[],
        'page': 2,
        'page_size': 50,
        'total': 60,
      };
    });
    final d = await repo.delivery(const DeliveryQuery('r1', state: 'not_viewed', q: ' asha '), page: 2);
    expect(d.items.hasMore, isFalse);
    expect(a.calls.last.path, '/admin/payroll/runs/r1/delivery');
    expect(a.calls.last.query, {'state': 'not_viewed', 'q': 'asha', 'page': 2, 'page_size': 50});
    expect((await repo.remindDelivery('r1')).sent, 2);
    expect(a.calls.last.body, {'user_ids': null});
    await repo.remindDelivery('r1', userIds: ['u1']);
    expect(a.calls.last.body, {'user_ids': ['u1']});
  });

  test('bank accounts: list (items or bare list), create with password, partial update', () async {
    Map<String, dynamic> acc() => {
          'id': 'ba1',
          'label': 'Main',
          'bank_name': 'HDFC Bank',
          'account': 'XXXXXX4321',
          'ifsc': 'HDFC0001234',
          'is_default': true,
          'active': true,
        };
    final (repo, a) = _repo((c) => c.method == 'GET' ? {'items': [acc()]} : acc());
    expect((await repo.bankAccounts()).single.account, 'XXXXXX4321');
    final (repo2, _) = _repo((c) => [acc()]);
    expect((await repo2.bankAccounts()).single.label, 'Main');
    await repo.createBankAccount(
      const BankAccountInput(label: 'Main', bankName: 'HDFC Bank', account: '123456789012', ifsc: 'HDFC0001234'),
      currentPassword: 'pw',
    );
    expect(a.calls.last.method, 'POST');
    expect(a.calls.last.path, '/admin/payroll/bank-accounts');
    expect((a.calls.last.body! as Map)['current_password'], 'pw');
    await repo.updateBankAccount('ba1', active: false);
    expect(a.calls.last.method, 'PUT');
    expect(a.calls.last.path, '/admin/payroll/bank-accounts/ba1');
    expect(a.calls.last.body, {'active': false});
    await repo.updateBankAccount('ba1', label: ' New ', isDefault: true);
    expect(a.calls.last.body, {'label': 'New', 'is_default': true});
    await repo.updateBankAccount('ba1', isDefault: true, currentPassword: 'pw'); // a default change is stepped up
    expect(a.calls.last.body, {'is_default': true, 'current_password': 'pw'});
  });

  test('payment batches: create, list, edit, submit, approve, reject, cancel, confirm', () async {
    final (repo, a) = _repo((c) {
      if (c.path == '/admin/payroll/payment-batches') {
        return {'items': [batchJson()], 'page': 1, 'page_size': 20, 'total': 1};
      }
      return batchJson();
    });
    final b = await repo.createPaymentBatch('r1', const PaymentBatchCreate(valueDate: '2026-10-31'));
    expect(b.reference, 'PAY-2026-10-01');
    expect(a.calls.last.path, '/admin/payroll/runs/r1/payment-batches');
    expect(a.calls.last.body, {'debit_account_id': null, 'value_date': '2026-10-31', 'user_ids': null});
    final page = await repo.paymentBatches(const PaymentBatchQuery(status: 'submitted'));
    expect(page.items.single.lines, hasLength(2));
    expect(a.calls.last.query, {'status': 'submitted', 'page': 1, 'page_size': 20});
    await repo.paymentBatch('b1');
    await repo.updatePaymentBatch('b1', const PaymentBatchEdit(), expectedVersion: 3);
    expect(a.calls.last.method, 'PUT');
    expect(a.calls.last.body, {'expected_version': 3});
    await repo.submitPaymentBatch('b1', expectedVersion: 3);
    expect(a.calls.last.path, '/admin/payroll/payment-batches/b1/submit');
    await repo.withdrawPaymentBatch('b1', expectedVersion: 3);
    expect(a.calls.last.path, '/admin/payroll/payment-batches/b1/withdraw');
    await repo.approvePaymentBatch('b1', expectedVersion: 2, currentPassword: 'pw', comment: '');
    expect(a.calls.last.body, {'expected_version': 2, 'comment': null, 'current_password': 'pw'});
    await repo.rejectPaymentBatch('b1', expectedVersion: 2, comment: 'wrong date');
    expect(a.calls.last.body, {'expected_version': 2, 'comment': 'wrong date'});
    await repo.cancelPaymentBatch('b1', ' duplicate ');
    expect(a.calls.last.body, {'reason': 'duplicate'});
    await repo.confirmPayment(
      'b1',
      const PaymentConfirm(paidOn: '2026-10-31', allPaid: true),
      currentPassword: 'pw',
    );
    expect(a.calls.last.path, '/admin/payroll/payment-batches/b1/confirm');
    expect((a.calls.last.body! as Map)['current_password'], 'pw');
    expect((a.calls.last.body! as Map)['all_paid'], true);
  });

  test('bank file: POST with the password, bytes and a safe csv name', () async {
    final a = _Adapter((c) => (
          200,
          'Sr No,Payment Type\r\n1,NEFT\r\n',
          <String, List<String>>{
            Headers.contentTypeHeader: ['text/csv; charset=us-ascii'],
            'content-disposition': ['attachment; filename="bank-PAY-2026-10-01.csv"'],
          },
        ),);
    final repo = PayrollRepository(Dio(BaseOptions(baseUrl: 'http://x/api'))..httpClientAdapter = a);
    final f = await repo.paymentFile('b1', currentPassword: 'pw');
    expect(f.filename, 'bank-PAY-2026-10-01.csv');
    expect(utf8.decode(f.bytes), startsWith('Sr No,'));
    expect(a.calls.single.method, 'POST');
    expect(a.calls.single.path, '/admin/payroll/payment-batches/b1/file');
    expect(a.calls.single.body, {'current_password': 'pw'});
  });

  test('file errors keep their code (NOT_APPROVED)', () async {
    final a = _Adapter((c) => (
          409,
          {
            'error': {'code': 'NOT_APPROVED', 'message': 'x', 'details': <Object>[], 'request_id': 'q'},
          },
          <String, List<String>>{},
        ),);
    final repo = PayrollRepository(Dio(BaseOptions(baseUrl: 'http://x/api'))..httpClientAdapter = a);
    await expectLater(
      repo.paymentFile('b1', currentPassword: 'pw'),
      throwsA(isA<ApiException>().having((e) => e.code, 'code', 'NOT_APPROVED')),
    );
  });
}
