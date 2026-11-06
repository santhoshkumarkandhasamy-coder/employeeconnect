import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/payroll/data/payroll_repository.dart';
import 'package:employee_connect/features/payroll/domain/phase5c_models.dart';
import 'package:flutter_test/flutter_test.dart';

import 'payroll_phase5c_logic_test.dart' show tdsJson;

/// Paths, bodies and parsing of the 5c calls (api-contract.md 19.1 [5c], 19.9) against a stubbed transport.
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
    if (json is List<int>) {
      return ResponseBody.fromBytes(json, status, headers: headers);
    }
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

(PayrollRepository, _Adapter) _repo(Object? Function(_Call) reply, {int status = 200, Map<String, List<String>>? headers}) {
  final a = _Adapter((c) => (status, reply(c), headers ?? <String, List<String>>{}));
  final dio = Dio(BaseOptions(baseUrl: 'http://x/api'))..httpClientAdapter = a;
  return (PayrollRepository(dio), a);
}

void main() {
  test('preview posts the user, fy, period and regime and parses both regimes', () async {
    final (repo, a) = _repo((c) => {
          'computation': tdsJson(),
          'other_regime': {'regime': 'old', 'total_tax': '12500.00'},
        },);
    final r = await repo.tdsPreview(userId: 'u1', fy: '2026-27', period: '2026-11', regime: 'old');
    expect(a.calls.single.method, 'POST');
    expect(a.calls.single.path, '/admin/payroll/tax/preview');
    expect(a.calls.single.body, {'user_id': 'u1', 'fy': '2026-27', 'period': '2026-11', 'regime': 'old'});
    expect(r.computation.suggested, '2500.00');
    expect(r.otherRegime!.totalTax, '12500.00');
  });

  test('preview without options sends nulls', () async {
    final (repo, a) = _repo((c) => {'computation': tdsJson()});
    await repo.tdsPreview(userId: 'u1');
    expect(a.calls.single.body, {'user_id': 'u1', 'fy': null, 'period': null, 'regime': null});
  });

  test('summary sends filters and page, and reads the totals', () async {
    final (repo, a) = _repo((c) => {
          'items': <Object?>[],
          'page': 2,
          'page_size': 20,
          'total': 25,
          'totals': {'total_tax': '1.00', 'tds_paid_so_far': '2.00', 'remaining_tax': '3.00'},
        },);
    final p = await repo.tdsSummary(const TdsSummaryQuery(fy: '2026-27', q: ' asha ', flag: 'TDS_EXCESS'), page: 2);
    expect(a.calls.single.method, 'GET');
    expect(a.calls.single.path, '/admin/payroll/tax/summary');
    expect(a.calls.single.query, {'fy': '2026-27', 'q': 'asha', 'flag': 'TDS_EXCESS', 'page': 2, 'page_size': 20});
    expect(p.remainingTax, '3.00');
    expect(p.page, 2);
  });

  test('the statement is a POST with the password in the body, never in the URL', () async {
    final (repo, a) = _repo(
      (c) => [37, 80, 68, 70],
      headers: {
        'content-disposition': ['attachment; filename="tax-computation-EMP001-2026-27.pdf"'],
      },
    );
    final f = await repo.taxStatement('u1', fy: '2026-27', currentPassword: 'pw');
    expect(a.calls.single.method, 'POST');
    expect(a.calls.single.path, '/admin/payroll/tax/employees/u1/statement?fy=2026-27');
    expect(a.calls.single.path, isNot(contains('pw')));
    expect(a.calls.single.body, {'current_password': 'pw'});
    expect(f.filename, 'tax-computation-EMP001-2026-27.pdf');
    expect(f.bytes, [37, 80, 68, 70]);
  });

  test('24Q posts the quarter and the password and keeps a safe file name', () async {
    final (repo, a) = _repo(
      (c) => [1, 2, 3],
      headers: {
        'content-disposition': ['attachment; filename="../../24q 2026-27 q3.xlsx"'],
      },
    );
    final f = await repo.tax24q(fy: '2026-27', quarter: 3, currentPassword: 'pw');
    expect(a.calls.single.path, '/admin/payroll/tax/24q?fy=2026-27&quarter=3');
    expect(a.calls.single.body, {'current_password': 'pw'});
    expect(f.filename, isNot(contains('/')));
    expect(f.filename, endsWith('.xlsx'));
  });

  test('the employee projection is a GET for the own year', () async {
    final (repo, a) = _repo((c) => {
          'estimate': true,
          'computation': tdsJson(),
          'other_regime': {'regime': 'old', 'total_tax': '9.00'},
        },);
    final r = await repo.myTaxProjection(fy: '2026-27');
    expect(a.calls.single.method, 'GET');
    expect(a.calls.single.path, '/me/tax/projection');
    expect(a.calls.single.query, {'fy': '2026-27'});
    expect(r.estimate, isTrue);
  });

  test('409 TDS_ENGINE_OFF surfaces as an ApiException with its code', () async {
    final (repo, _) = _repo(
      (c) => {
        'error': {'code': 'TDS_ENGINE_OFF', 'message': 'off', 'details': <Object?>[], 'request_id': 'r'},
      },
      status: 409,
    );
    await expectLater(
      repo.myTaxProjection(),
      throwsA(isA<ApiException>().having((e) => e.code, 'code', 'TDS_ENGINE_OFF')),
    );
  });

  group('TDS challan body', () {
    Future<Map<String, Object?>> body({
      required String type,
      required bool paid,
      String? bsr,
      String? serial,
      String? deposited,
    }) async {
      final (repo, a) = _repo((c) => {'ok': true});
      await repo.setChallan(
        type: type,
        period: '2026-10',
        paid: paid,
        currentPassword: 'pw',
        paidOn: '2026-10-07',
        reference: 'CH1',
        bsrCode: bsr,
        challanSerial: serial,
        depositedOn: deposited,
      );
      expect(a.calls.single.method, 'PUT');
      expect(a.calls.single.path, '/admin/payroll/statutory/challans');
      return (a.calls.single.body as Map).cast<String, Object?>();
    }

    test('a paid TDS challan carries BSR code, serial and deposit date', () async {
      final b = await body(type: 'tds', paid: true, bsr: '0510016', serial: '00042', deposited: '2026-10-06');
      expect(b['bsr_code'], '0510016');
      expect(b['challan_serial'], '00042');
      expect(b['deposited_on'], '2026-10-06');
      expect(b['status'], 'paid');
    });

    test('other types and unpaid never send them', () async {
      final pf = await body(type: 'pf', paid: true, bsr: '0510016', serial: '1', deposited: '2026-10-06');
      expect(pf.keys, isNot(contains('bsr_code')));
      expect(pf.keys, isNot(contains('challan_serial')));
      expect(pf.keys, isNot(contains('deposited_on')));
      final unpaid = await body(type: 'tds', paid: false, bsr: '0510016', serial: '1', deposited: '2026-10-06');
      expect(unpaid.keys, isNot(contains('bsr_code')));
      expect(unpaid['status'], 'unpaid');
    });
  });
}
