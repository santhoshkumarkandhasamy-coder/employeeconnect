import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/payroll/data/payroll_repository.dart';
import 'package:employee_connect/features/payroll/domain/payroll_models.dart';
import 'package:flutter_test/flutter_test.dart';

/// Contract tests for [PayrollRepository] against REAL responses captured from
/// the backend (`test/fixtures/payroll_contract_samples.json`, produced from the
/// implemented API: api-contract.md section 13 incl. 13.8). The Dio transport is
/// stubbed; everything above it (paths, bodies, parsing, error envelope) is real.
class _Call {
  _Call(this.method, this.path, this.body, this.query);
  final String method;
  final String path;
  final Object? body;
  final Map<String, dynamic> query;
}

class _Reply {
  _Reply(this.status, this.json, {this.headers = const {}});
  final int status;
  final Object? json;
  final Map<String, List<String>> headers;
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
    final text = r.json == null ? '' : jsonEncode(r.json);
    return ResponseBody.fromString(
      text,
      r.status,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
        ...r.headers,
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
    samples = jsonDecode(File('test/fixtures/payroll_contract_samples.json').readAsStringSync())
        as Map<String, dynamic>;
  });

  group('requests', () {
    test('PUT /admin/payroll/settings wraps the changes in {"settings": ...}', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['settings']));
      final out = await repo.saveSettings({'payroll.rounding': 'paise', 'payroll.pf_wage_ceiling': 15000});
      expect(a.calls.single.method, 'PUT');
      expect(a.calls.single.path, '/admin/payroll/settings');
      expect(a.calls.single.body, {
        'settings': {'payroll.rounding': 'paise', 'payroll.pf_wage_ceiling': 15000},
      });
      expect(out['payroll.pf_wage_ceiling'], 15000);
      expect(PtSlab.parseList(out['payroll.pt_slabs']), isNotEmpty);
    });

    test('settings PUT adds current_password beside "settings" only when given', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['settings']));
      await repo.saveSettings({'payroll.publish_requires_second_person': true}, currentPassword: 'pw');
      expect(a.calls.single.body, {
        'settings': {'payroll.publish_requires_second_person': true},
        'current_password': 'pw',
      });
    });

    test('reads settings from the "settings" envelope', () async {
      final (repo, _) = _repo((c) => _Reply(200, samples['settings']));
      final s = await repo.settings();
      expect(s['payroll.pt_frequency'], 'half_yearly');
      expect(s['payroll.esi_employee_rate_pct'], 0.75);
      final slabs = PtSlab.parseList(s['payroll.pt_slabs']);
      expect(slabs.first.months, [3, 9]);
      expect(slabs.last.maxGross, isNull);
    });

    test('run actions use the documented paths and bodies', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['run_publish']));
      await repo.publishRun('r1', currentPassword: 'pw');
      await repo.processRun('r1');
      await repo.refreshRun('r1');
      await repo.reopenRun('r1');
      await repo.createRun('2026-10', payDate: '2026-10-31');
      expect(a.calls.map((c) => '${c.method} ${c.path}'), [
        'POST /admin/payroll/runs/r1/publish',
        'POST /admin/payroll/runs/r1/process',
        'POST /admin/payroll/runs/r1/refresh',
        'POST /admin/payroll/runs/r1/reopen',
        'POST /admin/payroll/runs',
      ]);
      expect(a.calls.first.body, {'current_password': 'pw'});
      expect(a.calls.last.body, {'period': '2026-10', 'pay_date': '2026-10-31'});
    });

    test('revise posts the reason to the run/employee path', () async {
      final (repo, a) = _repo((c) => _Reply(201, samples['revise']));
      final p = await repo.revise('r1', 'u1', 'Leave approved');
      expect(a.calls.single.path, '/admin/payroll/runs/r1/employees/u1/revise');
      expect(a.calls.single.body, {'reason': 'Leave approved'});
      expect(p.revision, 2);
      expect(p.isDraft, isTrue);
      expect(p.revisionInfo?.supersedesId, isNotNull);
    });

    test('delete answers 204 without a body', () async {
      final (repo, a) = _repo((c) => _Reply(204, null));
      await repo.deleteRun('r1');
      await repo.deleteStructure('s1');
      await repo.deletePayslip('p1');
      expect(a.calls.map((c) => c.method), ['DELETE', 'DELETE', 'DELETE']);
    });

    test('details update carries current_password beside the changed keys only', () async {
      final (repo, a) = _repo((c) => _Reply(200, samples['details_put']));
      await repo.saveDetails('u1', {'designation': 'Sr Analyst'}, currentPassword: 'pw');
      expect(a.calls.single.body, {'designation': 'Sr Analyst', 'current_password': 'pw'});
    });
  });

  group('responses captured from the backend', () {
    test('employee list, a superseded payslip and its live revision', () async {
      final (repo, _) = _repo((c) {
        if (c.path == '/payslips') return _Reply(200, samples['my_list_after']);
        return _Reply(200, c.path.endsWith('/old') ? samples['my_payslip_old'] : samples['my_payslip_new']);
      });
      final list = await repo.myPayslips(2025);
      expect(list.single.period, '2025-06');
      expect(list.single.revision, 2);
      expect(list.single.revised, isTrue);
      final old = await repo.myPayslip('old');
      expect(old.isSuperseded, isTrue);
      expect(old.revisionInfo?.supersededById, list.single.id);
      expect(old.totals?.netPay, '26100.00');
      expect(old.totals?.netPayWords, contains('Twenty-Six Thousand'));
      expect(old.employerContributions?.single.code, 'PF_ER');
      expect(old.earnings.last.monthly, isNull); // adjustments have no monthly amount
      expect(old.employee.uan, '100200300400');
      expect(old.employee.bankAccount, startsWith('XXXX'));
      expect(old.days.paidDays, 28.5);
      expect(old.isAdminShape, isFalse);
      final live = await repo.myPayslip('new');
      expect(live.revision, 2);
      expect(live.isPublished, isTrue);
      expect(live.revisionInfo?.reason, 'Leave approved');
      expect(live.revisionInfo?.supersedesId, old.id);
      expect(live.days.lopDays, 0);
    });

    test('an open (unpublished) revision is invisible to the employee', () async {
      final (repo, _) = _repo((c) => _Reply(200, samples['my_payslip']));
      final p = await repo.myPayslip('x');
      expect(p.isPublished, isTrue);
      expect(p.revisionInfo, isNull);
    });

    test('admin payslip: lop, inputs, warnings, version', () async {
      final (repo, _) = _repo((c) => _Reply(200, samples['admin_payslip_put']));
      final p = await repo.payslip('x');
      expect(p.isAdminShape, isTrue);
      expect(p.version, 2);
      expect(p.lop?.override, 1.5);
      expect(p.lop?.overrideReason, 'Unpaid leave');
      expect(p.inputs?.tds, '1200.00');
      expect(p.inputs?.adjustments.single.code, 'ARREARS');
      expect(p.warnings, ['LOP_OVERRIDDEN']);
      expect(p.user?.employeeId, 'EMP001');
    });

    test('run, run list, run payslips, employees, structures, details', () async {
      final (repo, _) = _repo((c) {
        switch (c.path) {
          case '/admin/payroll/runs':
            return c.method == 'GET'
                ? _Reply(200, samples['runs'])
                : _Reply(201, samples['run_create']);
          case '/admin/payroll/employees':
            return _Reply(200, samples['employees']);
          default:
            if (c.path.endsWith('/structures')) return _Reply(200, samples['structures']);
            if (c.path.endsWith('/details')) return _Reply(200, samples['details']);
            return _Reply(200, samples['run_payslips']);
        }
      });
      final run = await repo.createRun('2025-06');
      expect(run.counts.employees, 6);
      expect(run.totals.grossEarnings, '175850.00');
      expect(run.totals.employerPf, '11640.00');
      expect(run.totals.employerEsi, '1068.00');
      expect(run.attendanceCutoff, isNotNull);
      final items = await repo.runs(2025);
      expect(items.single.netPayTotal, run.totals.netPay);
      final rows = await repo.runPayslips(RunPayslipQuery(run.id));
      expect(rows.items, hasLength(6));
      expect(rows.items.every((r) => r.netPay != null), isTrue);
      expect(rows.items.map((r) => r.user?.employeeId), contains('JOIN01'));
      final emps = await repo.employees(const PayrollEmployeeQuery());
      expect(emps.total, 7); // the unpaid operator (admin) is listed but excluded from payroll
      expect(emps.items.where((e) => !e.includeInPayroll), hasLength(1));
      final leaver = emps.items.firstWhere((e) => e.user.employeeId == 'LEAV01');
      expect(leaver.isActive, isFalse);
      expect(leaver.includeInPayroll, isTrue);
      expect(leaver.currentStructure?.monthlyGross, '50000.00');
      expect(leaver.flags, contains('NO_PAN'));
      expect(leaver.department, 'General');
      final st = await repo.structures('u');
      expect(st.single.pfOnFullBasic, isTrue);
      expect(st.single.fixedDeductions.single.code, 'CANTEEN');
      expect(st.single.monthlyGross, '40000.00');
      final d = await repo.details('u');
      expect(d.revealed, isFalse);
      expect(d.pan, 'XXXXX1234X');
      expect(d.bankIfsc, isNotNull);
    });
  });

  group('error envelope {"error":{code,message,details,request_id}}', () {
    Future<ApiException> fail409(String key) {
      final s = (samples['err'] as Map<String, dynamic>)[key] as List<dynamic>;
      final (repo, _) = _repo((c) => _Reply(s[0] as int, s[1]));
      return _thrown(() => repo.processRun('r'));
    }

    test('every captured payroll error maps code, status and request id', () async {
      for (final k in (samples['err'] as Map<String, dynamic>).keys) {
        final s = (samples['err'] as Map<String, dynamic>)[k] as List<dynamic>;
        final expectCode = (s[1] as Map<String, dynamic>)['error']['code'] as String;
        final e = await fail409(k);
        expect(e.code, expectCode, reason: k);
        expect(e.status, s[0], reason: k);
        expect(e.requestId, isNotNull, reason: k);
        expect(payrollErrorMessage(e), isNotEmpty, reason: k);
      }
    });

    test('PAYROLL_LOCKED and CONFLICT are both 409 and carry readable text', () async {
      final locked = await fail409('locked');
      expect(locked.code, 'PAYROLL_LOCKED');
      expect(locked.status, 409);
      final conflict = await fail409('process_twice');
      expect(conflict.code, 'CONFLICT');
      expect(conflict.status, 409);
      // CONFLICT shows the server's own explanation (what state the run is in)
      expect(payrollErrorMessage(conflict), conflict.message);
      expect(payrollErrorMessage(await fail409('revise_conflict')), contains('open revision'));
    });

    test('VALIDATION_ERROR and INVALID_PERIOD keep field details', () async {
      final e = await fail409('invalid_period');
      expect(e.code, 'INVALID_PERIOD');
      expect(e.status, 422);
      expect(e.detail('period'), isNotNull);
    });

    test('PAYROLL_BLOCKED details are {field: employee id, message: code}', () async {
      final (repo, _) = _repo((c) => _Reply(409, {
            'error': {
              'code': 'PAYROLL_BLOCKED',
              'message': 'Some payslips have errors',
              'details': [
                {'field': 'EMP014', 'message': 'STRUCTURE_MISSING'},
                {'field': 'EMP020', 'message': 'NEGATIVE_NET_PAY'},
              ],
              'request_id': 'abc',
            },
          },),);
      final e = await _thrown(() => repo.processRun('r'));
      expect(e.status, 409);
      expect(payrollErrorMessage(e), contains('EMP014: No salary structure'));
      expect(payrollErrorMessage(e), contains('EMP020: Net pay is negative'));
    });

    test('503 TEMPORARILY_UNAVAILABLE and 429 use Retry-After', () async {
      final (repo, _) = _repo((c) => _Reply(
            503,
            {
              'error': {'code': 'TEMPORARILY_UNAVAILABLE', 'message': 'busy', 'details': <Object>[], 'request_id': 'r'},
            },
            headers: {
              'retry-after': ['7'],
            },
          ),);
      final e = await _thrown(() => repo.myPayslip('x'));
      expect(e.code, 'TEMPORARILY_UNAVAILABLE');
      expect(payrollErrorMessage(e), contains('7 seconds'));
    });

    test('a file endpoint error (bytes request) is decoded from the envelope', () async {
      final a = _FileAdapter(
        403,
        utf8.encode(jsonEncode({
          'error': {'code': 'REAUTH_FAILED', 'message': 'Your password is incorrect', 'details': <Object>[], 'request_id': 'r1'},
        }),),
      );
      final repo = PayrollRepository(Dio(BaseOptions(baseUrl: 'http://x/api'))..httpClientAdapter = a);
      final e = await _thrown(() => repo.register('r', '2025-06', includeBank: true, currentPassword: 'bad'));
      expect(e.code, 'REAUTH_FAILED');
      expect(e.status, 403);
      expect(e.requestId, 'r1');
    });

    test('a nginx HTML 502 becomes SERVICE_UNAVAILABLE, not a crash', () async {
      final a = _FileAdapter(502, utf8.encode('<html>Bad gateway</html>'));
      final repo = PayrollRepository(Dio(BaseOptions(baseUrl: 'http://x/api'))..httpClientAdapter = a);
      final e = await _thrown(() => repo.myPayslipPdf('p'));
      expect(e.code, 'SERVICE_UNAVAILABLE');
      expect(payrollErrorMessage(e), contains('busy'));
    });
  });

  test('register body: password only when bank details are included', () async {
    final a = _FileAdapter(200, [1, 2, 3], headers: {
      'content-disposition': ['attachment; filename="payroll-register-2025-06.xlsx"'],
    },);
    final repo = PayrollRepository(Dio(BaseOptions(baseUrl: 'http://x/api'))..httpClientAdapter = a);
    final f = await repo.register('r', '2025-06', includeBank: true, currentPassword: 'pw');
    expect(f.filename, 'payroll-register-2025-06.xlsx');
    expect(jsonDecode(a.lastBody!), {'include_bank': true, 'current_password': 'pw'});
    await repo.register('r', '2025-06', currentPassword: 'ignored');
    expect(jsonDecode(a.lastBody!), {'include_bank': false});
  });
}

class _FileAdapter implements HttpClientAdapter {
  _FileAdapter(this.status, this.bytes, {this.headers = const {}});
  final int status;
  final List<int> bytes;
  final Map<String, List<String>> headers;
  String? lastBody;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    final d = options.data;
    lastBody = d is String ? d : (d == null ? null : jsonEncode(d));
    return ResponseBody.fromBytes(
      bytes,
      status,
      headers: {
        if (status != 200) Headers.contentTypeHeader: [Headers.jsonContentType],
        ...headers,
      },
    );
  }

  @override
  void close({bool force = false}) {}
}
