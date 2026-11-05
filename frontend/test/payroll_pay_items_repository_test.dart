import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/payroll/data/payroll_repository.dart';
import 'package:employee_connect/features/payroll/domain/pay_items_models.dart';
import 'package:employee_connect/features/payroll/domain/payroll_models.dart' show payrollErrorMessage;
import 'package:flutter_test/flutter_test.dart';

/// Repository paths, bodies and parsing for contract section 17 (work package
/// 3a) against HAND-WRITTEN fixtures (`payroll_phase3_contract_samples.json`),
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
    samples = jsonDecode(File('test/fixtures/payroll_phase3_contract_samples.json').readAsStringSync())
        as Map<String, dynamic>;
  });

  group('one-time items', () {
    test('list sends period and only the filters that are set', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['items_page']));
      final p = await repo.oneTimeItems(const ItemQuery(period: '2026-10', code: 'BONUS'), page: 2);
      expect(a.calls.single.method, 'GET');
      expect(a.calls.single.path, '/admin/payroll/items');
      expect(a.calls.single.query, {'period': '2026-10', 'code': 'BONUS', 'page': 2, 'page_size': 20});
      expect(p.items, hasLength(3));
      expect(p.total, 3);
    });

    test('create posts exactly the contract keys (amount stays a string)', () async {
      final (repo, a) = _repo((c) => _Reply(201, samples['item']));
      final i = await repo.createItem(
        userId: 'u1',
        period: '2026-11',
        code: 'BONUS',
        label: 'Diwali bonus',
        amount: '5000.00',
      );
      expect(a.calls.single.path, '/admin/payroll/items');
      expect(a.calls.single.json, {
        'user_id': 'u1',
        'period': '2026-11',
        'code': 'BONUS',
        'label': 'Diwali bonus',
        'amount': '5000.00',
      });
      expect(i.version, 1);
      expect(i.runStatus, 'processed');
    });

    test('update sends expected_version and only the changed keys', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['item']));
      await repo.updateItem('i1', expectedVersion: 4, amount: '6000.00');
      expect(a.calls.single.method, 'PUT');
      expect(a.calls.single.path, '/admin/payroll/items/i1');
      expect(a.calls.single.json, {'expected_version': 4, 'amount': '6000.00'});
    });

    test('cancel posts the reason', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['item']));
      await repo.cancelItem('i1', 'Duplicate entry');
      expect(a.calls.single.path, '/admin/payroll/items/i1/cancel');
      expect(a.calls.single.json, {'reason': 'Duplicate entry'});
    });

    test('template is a GET download', () async {
      final (repo, a) = _repo((c) => _Reply(200, null));
      final f = await repo.itemsTemplate();
      expect(a.calls.single.method, 'GET');
      expect(a.calls.single.path, '/admin/payroll/items/template');
      expect(f.filename, 'payroll-items-template.xlsx');
    });

    test('error envelopes keep their codes', () async {
      final (repo, _) = _repo((c) => _Reply(409, samples['errors']['PERIOD_LOCKED']));
      final e = await _thrown(() => repo.cancelItem('i1', 'reason here'));
      expect(e.code, 'PERIOD_LOCKED');
      expect(payrollErrorText(e), contains('locked'));
    });
  });

  group('import', () {
    test('dry run is multipart with dry_run=true and no password or hash', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['import_dry_run']));
      final r = await repo.importDryRun(bytes: [1, 2, 3], filename: 'bonus.xlsx', period: '2026-11');
      final c = a.calls.single;
      expect(c.method, 'POST');
      expect(c.path, '/admin/payroll/items/import');
      expect(c.query, {'dry_run': true});
      expect(c.fields, {'period': '2026-11'});
      expect(c.form.files.single.key, 'file');
      expect(c.form.files.single.value.filename, 'bonus.xlsx');
      expect(c.form.files.single.value.length, 3);
      expect(r.errors, hasLength(2));
      expect(r.canCommit, isFalse);
    });

    test('commit re-sends the file with file_sha256 and current_password', () async {
      final (repo, a) = _repo((c) => _Reply(201, samples['import_commit']));
      final sha = 'a' * 64;
      final out = await repo.importCommit(
        bytes: [1, 2, 3],
        filename: 'bonus.xlsx',
        period: '2026-11',
        fileSha256: sha,
        currentPassword: 'my-pw',
      );
      final c = a.calls.single;
      expect(c.query, {'dry_run': false});
      expect(c.fields, {'period': '2026-11', 'file_sha256': sha, 'current_password': 'my-pw'});
      expect(c.form.files, hasLength(1));
      expect(out.batchId, 'b2');
      expect(out.created, 118);
    });

    test('IMPORT_INVALID keeps the per-row details', () async {
      final (repo, _) = _repo((c) => _Reply(422, samples['import_invalid_error']));
      final e = await _thrown(
        () => repo.importCommit(
          bytes: [1],
          filename: 'x.xlsx',
          period: '2026-11',
          fileSha256: 'a' * 64,
          currentPassword: 'pw',
        ),
      );
      expect(e.code, 'IMPORT_INVALID');
      final rows = e.details.map((d) => ImportRowError.fromJson({'field': d.field, 'message': d.message})).toList();
      expect(rows.map((r) => r.row), [14, 40]);
      expect(rows.map((r) => r.field), ['employee_id', 'amount']);
    });

    test('batches list and cancel', () async {
      final (repo, a) = _repo((c) => c.path.endsWith('/cancel')
          ? _Reply(200, samples['batch_cancel'])
          : _Reply(200, samples['batches']),);
      final b = await repo.importBatches(period: '2026-10');
      expect(a.calls.first.path, '/admin/payroll/items/batches');
      expect(a.calls.first.query, {'period': '2026-10'});
      expect(b, hasLength(2));
      final r = await repo.cancelBatch('b1', 'Wrong file');
      expect(a.calls.last.path, '/admin/payroll/items/batches/b1/cancel');
      expect(a.calls.last.json, {'reason': 'Wrong file'});
      expect(r.cancelled, 110);
      expect(r.notCancelled, 8);
    });
  });

  group('loans', () {
    test('list filters and parsing', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['loan_list']));
      final p = await repo.loans(const LoanQuery(status: 'active', kind: 'advance'));
      expect(a.calls.single.path, '/admin/payroll/loans');
      expect(a.calls.single.query, {'status': 'active', 'kind': 'advance', 'page': 1, 'page_size': 20});
      expect(p.items.single.reference, 'L-0007');
    });

    test('create carries the password in the body', () async {
      final (repo, a) = _repo((c) => _Reply(201, samples['loan_detail']));
      await repo.createLoan(
        userId: 'u1',
        kind: 'loan',
        principal: '30000.00',
        emi: '2500.00',
        startPeriod: '2026-11',
        reason: 'Medical emergency',
        currentPassword: 'pw',
      );
      expect(a.calls.single.path, '/admin/payroll/loans');
      expect(a.calls.single.json, {
        'user_id': 'u1',
        'kind': 'loan',
        'principal': '30000.00',
        'emi': '2500.00',
        'start_period': '2026-11',
        'reason': 'Medical emergency',
        'current_password': 'pw',
      });
    });

    test('skip has no password; reschedule, foreclose and cancel do', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['loan_detail']));
      await repo.skipInstallment('l1', installmentId: 'n4', reason: 'Festival month', expectedVersion: 3);
      await repo.rescheduleLoan(
        'l1',
        emi: '2000.00',
        fromPeriod: '2026-12',
        reason: 'Lower EMI',
        expectedVersion: 3,
        currentPassword: 'pw',
      );
      await repo.forecloseLoan(
        'l1',
        mode: 'payroll',
        period: '2026-12',
        reason: 'Settle',
        expectedVersion: 3,
        currentPassword: 'pw',
      );
      await repo.forecloseLoan(
        'l1',
        mode: 'outside',
        paidOn: '2026-11-20',
        reference: 'UTR123',
        reason: 'Paid by transfer',
        expectedVersion: 3,
        currentPassword: 'pw',
      );
      await repo.cancelLoan('l1', reason: 'Entered by mistake', expectedVersion: 3, currentPassword: 'pw');
      expect(a.calls.map((c) => c.path), [
        '/admin/payroll/loans/l1/skip',
        '/admin/payroll/loans/l1/reschedule',
        '/admin/payroll/loans/l1/foreclose',
        '/admin/payroll/loans/l1/foreclose',
        '/admin/payroll/loans/l1/cancel',
      ]);
      expect(a.calls[0].json, {'installment_id': 'n4', 'reason': 'Festival month', 'expected_version': 3});
      expect(a.calls[1].json, {
        'emi': '2000.00',
        'from_period': '2026-12',
        'reason': 'Lower EMI',
        'expected_version': 3,
        'current_password': 'pw',
      });
      expect(a.calls[2].json, {
        'mode': 'payroll',
        'period': '2026-12',
        'reason': 'Settle',
        'expected_version': 3,
        'current_password': 'pw',
      });
      expect(a.calls[3].json, {
        'mode': 'outside',
        'paid_on': '2026-11-20',
        'reference': 'UTR123',
        'reason': 'Paid by transfer',
        'expected_version': 3,
        'current_password': 'pw',
      });
      expect(a.calls[4].json, {'reason': 'Entered by mistake', 'expected_version': 3, 'current_password': 'pw'});
    });

    test('REAUTH_FAILED and VERSION_CONFLICT surface as ApiException', () async {
      final (repo, _) = _repo((c) => _Reply(403, {
            'error': {'code': 'REAUTH_FAILED', 'message': 'no', 'request_id': 'r', 'details': <Object?>[]},
          },),);
      final e = await _thrown(
        () => repo.cancelLoan('l1', reason: 'reason here', expectedVersion: 1, currentPassword: 'bad'),
      );
      expect(e.code, 'REAUTH_FAILED');
      expect(e.status, 403);
    });

    test('employee loans use /me/loans and parse the reduced shape', () async {
      final (repo, a) = _repo((c) => c.path == '/me/loans'
          ? _Reply(200, samples['me_loans'])
          : _Reply(200, samples['me_loan']),);
      final list = await repo.myLoans();
      final d = await repo.myLoan('l1');
      expect(a.calls.map((c) => c.path), ['/me/loans', '/me/loans/l1']);
      expect(list, hasLength(2));
      expect(d.installments, hasLength(3));
    });
  });

  group('arrears', () {
    test('list, detail', () async {
      final (repo, a) = _repo((c) => c.path.endsWith('/ar1')
          ? _Reply(200, (samples['arrears_page']['items'] as List).first)
          : _Reply(200, samples['arrears_page']),);
      final p = await repo.arrears(const ArrearsQuery(status: 'proposed'));
      final d = await repo.arrearsDetail('ar1');
      expect(a.calls.first.query, {'status': 'proposed', 'page': 1, 'page_size': 20});
      expect(a.calls.last.path, '/admin/payroll/arrears/ar1');
      expect(p.items, hasLength(3));
      expect(d.breakdown, hasLength(3));
    });

    test('recalculate answers null when there is no difference', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['arrears_recalculate']));
      expect(await repo.recalculateArrears('u1'), isNull);
      expect(a.calls.single.method, 'POST');
      expect(a.calls.single.path, '/admin/payroll/employees/u1/arrears/recalculate');
    });

    test('recalculate returns the proposal when there is one', () async {
      final (repo, _) = _repo((c) => _Reply(200, {'arrears': (samples['arrears_page']['items'] as List).first}));
      final a = await repo.recalculateArrears('u1');
      expect(a!.amount, '4800.00');
    });

    test('accept sends period, expected_version and password; dismiss the reason', () async {
      final (repo, a) = _repo((c) => _Reply(200, (samples['arrears_page']['items'] as List).last));
      await repo.acceptArrears('ar1', period: '2026-11', expectedVersion: 2, currentPassword: 'pw');
      await repo.dismissArrears('ar1', 'Not applicable');
      expect(a.calls[0].path, '/admin/payroll/arrears/ar1/accept');
      expect(a.calls[0].json, {'period': '2026-11', 'expected_version': 2, 'current_password': 'pw'});
      expect(a.calls[1].path, '/admin/payroll/arrears/ar1/dismiss');
      expect(a.calls[1].json, {'reason': 'Not applicable'});
    });

    test('ARREARS_STALE is a conflict error with a readable text', () async {
      final (repo, _) = _repo((c) => _Reply(409, samples['errors']['ARREARS_STALE']));
      final e = await _thrown(
        () => repo.acceptArrears('ar1', period: '2026-11', expectedVersion: 1, currentPassword: 'pw'),
      );
      expect(e.code, 'ARREARS_STALE');
      expect(payrollErrorText(e), contains('Reload'));
    });
  });

  group('encashment', () {
    test('quote is a GET with the contract query', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['encashment_quote']));
      final q = await repo.encashmentQuote('u1', leaveTypeId: 'lt1', year: 2026, period: '2026-11', days: 4);
      expect(a.calls.single.method, 'GET');
      expect(a.calls.single.path, '/admin/payroll/employees/u1/encashment-quote');
      expect(a.calls.single.query, {'leave_type_id': 'lt1', 'year': 2026, 'period': '2026-11', 'days': 4.0});
      expect(q.amount, '2666.68');
    });

    test('quote without days leaves the key out', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['encashment_quote']));
      await repo.encashmentQuote('u1', leaveTypeId: 'lt1', year: 2026, period: '2026-11');
      expect(a.calls.single.query.containsKey('days'), isFalse);
    });

    test('create sends source=request and the password; cancel the optional reason', () async {
      final (repo, a) = _repo((c) => _Reply(201, (samples['encashments_page']['items'] as List).first));
      await repo.createEncashment(
        userId: 'u1',
        leaveTypeId: 'lt1',
        year: 2026,
        days: 4,
        period: '2026-11',
        reason: 'Year-end encashment',
        currentPassword: 'pw',
      );
      await repo.cancelEncashment('e1');
      await repo.cancelEncashment('e1', reason: 'Employee withdrew');
      expect(a.calls[0].path, '/admin/payroll/encashments');
      expect(a.calls[0].json, {
        'user_id': 'u1',
        'leave_type_id': 'lt1',
        'year': 2026,
        'days': 4.0,
        'period': '2026-11',
        'source': 'request',
        'reason': 'Year-end encashment',
        'current_password': 'pw',
      });
      expect(a.calls[1].path, '/admin/payroll/encashments/e1/cancel');
      expect(a.calls[1].body, anyOf(isNull, isEmpty));
      expect(a.calls[2].json, {'reason': 'Employee withdrew'});
    });

    test('INSUFFICIENT_BALANCE keeps available_days', () async {
      final (repo, _) = _repo((c) => _Reply(409, samples['encashment_insufficient_error']));
      final e = await _thrown(
        () => repo.encashmentQuote('u1', leaveTypeId: 'lt1', year: 2026, period: '2026-11', days: 9),
      );
      expect(e.detail('available_days'), '6.0');
      expect(payrollErrorText(e), contains('6.0 days'));
    });

    test('encashable leave types come from /admin/leave-types, filtered', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['leave_types_page']));
      final t = await repo.encashableLeaveTypes();
      expect(a.calls.single.path, '/admin/leave-types');
      expect(t.map((x) => x.code), ['EL']);
    });

    test('encashment list', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['encashments_page']));
      final p = await repo.encashments(const EncashmentQuery(year: 2026, status: 'pending'));
      expect(a.calls.single.query, {'year': 2026, 'status': 'pending', 'page': 1, 'page_size': 20});
      expect(p.items.single.days, 4.0);
    });
  });
}

String payrollErrorText(ApiException e) => payrollErrorMessage(e);
