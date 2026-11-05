import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/payroll/data/payroll_repository.dart';
import 'package:employee_connect/features/payroll/domain/gross_split.dart';
import 'package:employee_connect/features/payroll/domain/payroll_models.dart';
import 'package:employee_connect/features/payroll/domain/phase4a_models.dart';
import 'package:flutter_test/flutter_test.dart';

class _Call {
  _Call(this.method, this.path, this.body, this.query);
  final String method;
  final String path;
  final Object? body;
  final Map<String, dynamic> query;
  Map<String, dynamic> get json => (body! as Map).cast<String, dynamic>();
  Map<String, String> get fields => {for (final e in (body! as FormData).fields) e.key: e.value};
}

class _Adapter implements HttpClientAdapter {
  _Adapter(this.reply);
  final (int, Object?) Function(_Call) reply;
  final List<_Call> calls = [];

  @override
  Future<ResponseBody> fetch(RequestOptions o, Stream<Uint8List>? s, Future<void>? c) async {
    var body = o.data;
    if (body is String && body.isNotEmpty) body = jsonDecode(body);
    final call = _Call(o.method, o.path, body, o.queryParameters);
    calls.add(call);
    final r = reply(call);
    return ResponseBody.fromString(
      r.$2 == null ? '' : jsonEncode(r.$2),
      r.$1,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}

(PayrollRepository, _Adapter) _repo((int, Object?) Function(_Call) reply) {
  final a = _Adapter(reply);
  final dio = Dio(BaseOptions(baseUrl: 'http://x/api'))..httpClientAdapter = a;
  return (PayrollRepository(dio), a);
}

const _user = {'id': 'u1', 'employee_id': 'EMP001', 'full_name': 'Asha Rao'};

Map<String, dynamic> incJson({String status = 'draft', String kind = 'percent'}) => {
      'id': 'i1',
      'user': _user,
      'batch_id': null,
      'kind': kind,
      'effective_from': '2027-04-01',
      'percent': '10.00',
      'method': 'proportional',
      'base_structure': {'id': 's1', 'effective_from': '2026-04-01', 'monthly_gross': '40000.00'},
      'replaces_structure_id': null,
      'proposed': {
        'basic': '22000.00',
        'hra': '8800.00',
        'allowances': [
          {'code': 'SPECIAL', 'name': 'Special allowance', 'amount': '13200.00', 'prorate': true, 'esi_wage': true},
        ],
        'fixed_deductions': <Object?>[],
        'tds_monthly': '1200.00',
        'pf_applicable': true,
        'pf_on_full_basic': false,
        'esi_applicable': true,
        'pt_applicable': true,
        'monthly_gross': '44000.00',
      },
      'change': {'old_gross': '40000.00', 'new_gross': '44000.00', 'amount': '4000.00', 'percent': '10.00'},
      'diff': [
        {'code': 'BASIC', 'old': '20000.00', 'new': '22000.00'},
      ],
      'warnings': ['BACKDATED', 'TDS_REVIEW'],
      'reason': 'Annual increment 2027',
      'status': status,
      'run_status': 'none',
      'structure_id': null,
      'arrears_proposed': null,
      'created_by': {'id': 'm1', 'employee_id': 'EMP009', 'full_name': 'Maker'},
      'created_at': '2026-11-01T09:00:00Z',
      'submitted_by': null,
      'submitted_at': null,
      'decided_by': null,
      'decided_at': null,
      'decision_comment': null,
      'version': 2,
    };

void main() {
  group('models', () {
    test('Increment parses diff, warnings, proposed and change', () {
      final i = Increment.fromJson(incJson());
      expect(i.user.employeeId, 'EMP001');
      expect(i.change.percent, '10.00');
      expect(i.diff.single.newValue, '22000.00');
      expect(i.warnings, ['BACKDATED', 'TDS_REVIEW']);
      expect(i.proposed!.allowances.single.code, 'SPECIAL');
      expect(i.version, 2);
      expect(i.arrearsProposed, isNull);
      expect(i.proposedAsStructure()!.monthlyGross, '44000.00');
    });

    test('a remove increment has no proposed, diff or amounts', () {
      final j = incJson(kind: 'remove')
        ..['proposed'] = null
        ..['diff'] = <Object?>[]
        ..['change'] = {'old_gross': '40000.00', 'new_gross': null, 'amount': null, 'percent': null};
      final i = Increment.fromJson(j);
      expect(i.proposed, isNull);
      expect(i.change.isEmpty, isTrue);
    });

    test('CtcResult parses the contract example', () {
      final r = CtcResult.fromJson({
        'mode': 'from_ctc',
        'frequency': 'annual',
        'split': {
          'basic': '20000.00',
          'hra': '8000.00',
          'allowances': [
            {'code': 'SPECIAL', 'name': 'Special allowance', 'amount': '12000.00'},
          ],
        },
        'monthly': {
          'gross': '40000.00',
          'employer_pf': '1800.00',
          'employer_esi': '0.00',
          'employer_recovered': '0.00',
          'gratuity_accrual': '961.54',
          'ctc': '42761.54',
        },
        'annual': {'gross': '480000.00', 'ctc': '513138.48'},
        'employee': {
          'pf': '1800.00',
          'esi': '0.00',
          'pt_annual_estimate': '2500.00',
          'net_monthly_estimate': '37991.67',
        },
        'unallocated_monthly': '0.00',
        'flags': ['PF_CEILING_APPLIED'],
        'rules': {'pf_wage_ceiling': 15000},
      });
      expect(r.monthlyCtc, '42761.54');
      expect(r.annualCtc, '513138.48');
      expect(r.allowances.single.amount, '12000.00');
      expect(r.flags, ['PF_CEILING_APPLIED']);
      const input = CtcInput(mode: 'from_ctc', amount: '513138.48', frequency: 'annual');
      final s = r.toStructureInput(input, '2026-11-01');
      expect(s.monthlyGross, '40000.00');
    });

    test('GratuityPage parses items and totals', () {
      final g = GratuityPage.fromJson({
        'as_of': '2026-11-30',
        'basis': 'basic',
        'cap': '2000000.00',
        'min_years': 5,
        'items': [
          {
            'user': _user,
            'join_date': '2019-06-01',
            'exit_date': null,
            'service': {'years': 7, 'months': 5, 'days': 29},
            'counted_years': 7,
            'eligible': true,
            'wage': '22000.00',
            'accrued': '88846.00',
            'monthly_accrual': '1057.69',
            'flags': <Object?>[],
          },
          {
            'user': _user,
            'join_date': null,
            'service': {'years': 0, 'months': 0, 'days': 0},
            'counted_years': 0,
            'eligible': false,
            'wage': null,
            'accrued': null,
            'monthly_accrual': null,
            'flags': ['NO_JOIN_DATE'],
          },
        ],
        'totals': {'accrued': '1250000.00', 'eligible_accrued': '910000.00', 'monthly_accrual': '45000.00'},
        'page': 1,
        'page_size': 50,
        'total': 48,
      });
      expect(g.items.first.serviceLabel, '7y 5m 29d');
      expect(g.items.last.accrued, isNull);
      expect(g.accrued, '1250000.00');
      expect(g.hasMore, isFalse);
    });

    test('batch, bulk result and structure import report', () {
      final b = IncrementBatch.fromJson({
        'id': 'b1',
        'kind': 'percent',
        'effective_from': '2027-04-01',
        'percent': '8.00',
        'method': 'proportional',
        'rows': 46,
        'reason': 'x',
        'counts': {'draft': 0, 'submitted': 44, 'cancelled': 2},
        'totals': {
          'old_gross': '1850000.00',
          'new_gross': '1998000.00',
          'delta_monthly': '148000.00',
          'delta_annual': '1776000.00',
        },
        'file_sha256': null,
        'created_by': _user,
        'created_at': '2026-11-01T09:00:00Z',
        'cancelled_at': null,
      });
      expect(b.submittedCount, 44);
      expect(b.totals.deltaAnnual, '1776000.00');
      final r = BulkHikeResult.fromJson({
        'batch': {'id': 'b1', 'kind': 'percent', 'rows': 46, 'counts': <String, Object?>{}, 'totals': <String, Object?>{}},
        'created': 46,
        'skipped': [
          {'user': _user, 'reason': 'NO_STRUCTURE'},
        ],
      });
      expect(r.skipped.single.reason, 'NO_STRUCTURE');
      final rep = StructureImportReport.fromJson({
        'file_sha256': 'a' * 64,
        'rows': 120,
        'valid': 118,
        'errors': [
          {'row': 14, 'sheet': 'Structures', 'field': 'employee_id', 'message': 'UNKNOWN_EMPLOYEE'},
        ],
        'warnings': [
          {'row': 22, 'sheet': 'Structures', 'field': 'effective_from', 'message': 'BACKDATED'},
        ],
        'actions': {'create': 80, 'increment': 38},
        'totals': {'monthly_gross': '4200000.00'},
      });
      expect(rep.canCommit, isFalse);
      expect(rep.incrementCount, 38);
      expect(rep.errors.single.sheet, 'Structures');
      final issue = StructureImportIssue.fromCommitDetail('Structures row 14: employee_id', 'UNKNOWN_EMPLOYEE');
      expect([issue.sheet, issue.row, issue.field], ['Structures', 14, 'employee_id']);
    });

    test('salary history reads both entry types', () {
      final h = SalaryHistoryEntry.parseList({
        'items': [
          {'type': 'increment', 'increment_id': 'i1', 'effective_from': '2027-04-01', 'status': 'submitted', 'kind': 'percent', 'new_gross': '44000.00'},
          {'type': 'structure', 'structure_id': 's1', 'effective_from': '2026-04-01', 'effective_to': null, 'monthly_gross': '40000.00', 'change_percent': '8.11', 'source': 'increment', 'increment_id': 'i0', 'approved_by': _user},
        ],
      });
      expect(h.first.isOpenIncrement, isTrue);
      expect(h.first.monthlyGross, '44000.00');
      expect(h.last.source, 'increment');
      expect(h.last.approvedBy!.fullName, 'Asha Rao');
    });

    test('SalaryStructure reads source and increment_id', () {
      final s = SalaryStructure.fromJson({'id': 's', 'source': 'import', 'increment_id': 'i'});
      expect(s.source, 'import');
      expect(s.incrementId, 'i');
      expect(SalaryStructure.fromJson({'id': 's'}).source, 'manual');
    });
  });

  group('request bodies', () {
    test('percent, amount and remove send only their own fields', () {
      final p = IncrementInput.percent(
        effectiveFrom: '2027-04-01',
        percent: '10.00',
        method: 'proportional',
        reason: ' Annual ',
      ).toJson(expectedVersion: 3);
      expect(p.keys.toSet(), {'expected_version', 'kind', 'effective_from', 'percent', 'method', 'reason'});
      expect(p['reason'], 'Annual');
      final r = IncrementInput.remove(replacesStructureId: 's1', reason: 'Entered in error').toJson();
      expect(r.keys.toSet(), {'kind', 'replaces_structure_id', 'reason'});
      final a = IncrementInput.amount(
        effectiveFrom: '2027-04-01',
        monthlyGross: '44000.00',
        method: 'split_from_gross',
        reason: 'Promotion',
      ).toJson();
      expect(a.containsKey('percent'), isFalse);
    });

    test('structure kind nests the structure without effective_from', () {
      const s = StructureInput(
        effectiveFrom: '2027-04-01',
        basic: '22000.00',
        hra: '8800.00',
        allowances: [],
        fixedDeductions: [],
        tdsMonthly: '0.00',
        pfApplicable: true,
        pfOnFullBasic: false,
        esiApplicable: true,
        ptApplicable: true,
      );
      final j = IncrementInput.structure(structure: s, reason: 'Correction').toJson();
      expect(j['effective_from'], '2027-04-01');
      expect((j['structure']! as Map).containsKey('effective_from'), isFalse);
      expect(j['replaces_structure_id'], isNull);
    });

    test('bulk hike body: empty lists become null scope, overrides listed', () {
      const b = BulkHikeInput(
        effectiveFrom: '2027-04-01',
        percent: '8.00',
        method: 'proportional',
        reason: 'FY',
        departmentIds: [],
        overrides: {'u2': '12.00'},
      );
      final j = b.toJson();
      final scope = j['scope']! as Map;
      expect(scope['department_ids'], isNull);
      expect(scope['user_ids'], isNull);
      expect(scope['exclude_user_ids'], <String>[]);
      expect((j['overrides']! as List).single, {'user_id': 'u2', 'percent': '12.00'});
    });

    test('CtcInput sends null for company defaults', () {
      final j = const CtcInput(mode: 'from_gross', amount: '40000.00').toJson();
      expect(j['split'], isNull);
      expect(j['include_gratuity'], isNull);
      expect(j['employer_share_from_employee'], isNull);
      final c = const CtcInput(mode: 'from_gross', amount: '1.00', basicPct: 40, hraPctOfBasic: 50).toJson();
      expect(c['split'], {'basic_pct': 40, 'hra_pct_of_basic': 50});
    });
  });

  group('validation and helpers', () {
    test('percent validation', () {
      expect(validatePercent('', maxPct: 50), isNotNull);
      expect(validatePercent('0', maxPct: 50), isNotNull);
      expect(validatePercent('50.01', maxPct: 50), isNotNull);
      expect(validatePercent('8.555'), isNotNull);
      expect(validatePercent('50'), isNull);
      expect(validatePercent('0.01'), isNull);
      expect(normalizePercent('8'), '8.00');
      expect(normalizePercent('8.5'), '8.50');
    });

    test('percent preview rounds half-up to the rupee', () {
      expect(previewPercentGross('40000.00', '10.00'), '44000.00');
      expect(previewPercentGross('33333.00', '10.00'), '36666.00'); // 36666.3
      expect(previewPercentGross('10005.00', '10.00'), '11006.00'); // 11005.5
      expect(previewPercentGross('abc', '10'), isNull);
    });

    test('CTC amount limits', () {
      expect(validateCtcAmount('40000', annual: false), isNull);
      expect(validateCtcAmount('0.5', annual: false), isNotNull);
      expect(validateCtcAmount('10000001', annual: false), isNotNull);
      expect(validateCtcAmount('120000000', annual: true), isNull);
      expect(validateCtcAmount('120000001', annual: true), isNotNull);
    });

    test('big rupees formatting handles large and negative values', () {
      expect(formatBigRupees('120000000.00'), '₹12,00,00,000.00');
      expect(formatBigRupees('-1600.5'), '-₹1,600.50');
      expect(formatBigRupees(null), '-');
    });

    test('approve permission gate', () {
      expect(canApprovePayroll(['payroll.approve']), isTrue);
      expect(canApprovePayroll(['payroll.manage', 'payroll.view_all']), isFalse);
    });

    test('error messages for the new codes', () {
      String m(String c) => payrollErrorMessage(ApiException(code: c, message: ''));
      expect(m('APPROVAL_REQUIRED'), contains('increment'));
      expect(m('SECOND_PERSON_REQUIRED'), contains('different person'));
      expect(m('VERSION_CONFLICT'), contains('Reload'));
      expect(m('INCREMENT_STALE'), contains('changed'));
      expect(m('INCREMENT_EXISTS'), contains('already'));
      expect(m('NOT_SUBMITTED'), isNotEmpty);
      expect(m('LATER_STRUCTURE_EXISTS'), isNotEmpty);
      expect(m('AFTER_EXIT'), isNotEmpty);
    });

    test('split rule from settings, with fallbacks and a safe remainder', () {
      expect(SplitRule.fromSettings({}), SplitRule.defaults);
      final r = SplitRule.fromSettings({
        'payroll.split_basic_pct': 40,
        'payroll.split_hra_pct_of_basic': 50,
        'payroll.split_allowance_code': 'other',
        'payroll.split_allowance_name': 'Other pay',
      });
      expect([r.basicPct, r.hraPctOfBasic, r.allowanceCode, r.allowanceName], [40, 50, 'OTHER', 'Other pay']);
      final s = splitGross(10000, rule: r);
      expect([s.basic, s.hra, s.allowance], [4000, 2000, 4000]);
      expect(SplitRule.fromSettings({'payroll.split_basic_pct': 0}).basicPct, 50);
      final full = splitGross(1001, rule: const SplitRule(basicPct: 100, hraPctOfBasic: 40));
      expect(full.allowance >= 0 && full.total == 1001, isTrue);
    });
  });

  group('repository paths and bodies', () {
    test('increment calls', () async {
      final (repo, a) = _repo((c) => (c.method == 'GET' && c.path.endsWith('/increments')
          ? (200, {'items': [incJson()], 'page': 1, 'page_size': 20, 'total': 1})
          : (200, incJson(status: 'submitted'))),);
      final page = await repo.increments(const IncrementQuery(status: 'submitted', effectiveFrom: '2027-04'));
      expect(page.items.single.id, 'i1');
      expect(a.calls.last.query['status'], 'submitted');
      expect(a.calls.last.query['effective_from'], '2027-04');
      await repo.createIncrement(
        'u1',
        IncrementInput.percent(effectiveFrom: '2027-04-01', percent: '10.00', method: 'proportional', reason: 'abc'),
      );
      expect(a.calls.last.path, '/admin/payroll/employees/u1/increments');
      await repo.submitIncrement('i1', expectedVersion: 1);
      expect(a.calls.last.json, {'expected_version': 1});
      await repo.approveIncrement('i1', expectedVersion: 2, currentPassword: 'pw');
      expect(a.calls.last.path, '/admin/payroll/increments/i1/approve');
      expect(a.calls.last.json, {'expected_version': 2, 'comment': null, 'current_password': 'pw'});
      await repo.rejectIncrement('i1', expectedVersion: 2, comment: ' no ');
      expect(a.calls.last.json, {'expected_version': 2, 'comment': 'no'});
      await repo.cancelIncrement('i1', 'why');
      expect(a.calls.last.json, {'reason': 'why'});
    });

    test('batches, bulk, ctc, gratuity and history', () async {
      final batch = {
        'id': 'b1', 'kind': 'percent', 'rows': 1, 'counts': <String, Object?>{}, 'totals': <String, Object?>{},
      };
      final (repo, a) = _repo((c) {
        if (c.path.endsWith('/increments/bulk')) return (201, {'batch': batch, 'created': 1, 'skipped': <Object?>[]});
        if (c.path.endsWith('/approve')) return (200, {'approved': 1, 'failed': <Object?>[], 'arrears_proposed': 0});
        if (c.path.endsWith('/reject')) return (200, {'rejected': 4});
        if (c.path.endsWith('/submit')) return (200, {'submitted': 1, 'skipped': <Object?>[]});
        if (c.path.endsWith('/cancel')) return (200, {'cancelled': 2, 'not_cancelled': 1});
        if (c.path.endsWith('/gratuity')) {
          return (200, {'as_of': '2026-11-01', 'items': <Object?>[], 'totals': <String, Object?>{}, 'total': 0});
        }
        if (c.path.endsWith('/salary-history')) return (200, {'items': <Object?>[]});
        return (200, <String, Object?>{});
      });
      await repo.bulkHike(const BulkHikeInput(effectiveFrom: '2027-04-01', percent: '8.00', method: 'proportional', reason: 'abc'));
      expect(a.calls.last.path, '/admin/payroll/increments/bulk');
      expect((await repo.approveIncrementBatch('b1', currentPassword: 'pw', expected: [(id: 'i1', version: 2)])).approved, 1);
      expect(a.calls.last.json['current_password'], 'pw');
      expect(a.calls.last.json['expected'], [{'id': 'i1', 'version': 2}]);
      expect(await repo.rejectIncrementBatch('b1', comment: 'no'), 4);
      expect((await repo.submitIncrementBatch('b1')).submitted, 1);
      expect(a.calls.last.json, <String, Object?>{});
      expect((await repo.cancelIncrementBatch('b1', 'x')).notCancelled, 1);
      await repo.gratuity(const GratuityQuery(asOf: '2026-11-01', eligibleOnly: true));
      expect(a.calls.last.query['as_of'], '2026-11-01');
      expect(a.calls.last.query['eligible_only'], true);
      await repo.salaryHistory('u1');
      expect(a.calls.last.path, '/admin/payroll/employees/u1/salary-history');
      await repo.ctcCalculate(const CtcInput(mode: 'from_gross', amount: '40000.00'));
      expect(a.calls.last.path, '/admin/payroll/ctc/calculate');
      expect(a.calls.last.json['mode'], 'from_gross');
    });

    test('structure import dry run and commit', () async {
      final (repo, a) = _repo((c) => c.query['dry_run'] == true
          ? (200, {'file_sha256': 'a' * 64, 'rows': 1, 'valid': 1, 'errors': <Object?>[], 'warnings': <Object?>[], 'actions': {'create': 1, 'increment': 0}, 'totals': {'monthly_gross': '1.00'}})
          : (201, {'batch_id': 'b', 'created': 1, 'increments_submitted': 0, 'arrears_proposed': 0},),);
      final rep = await repo.structureImportDryRun(bytes: [1, 2], filename: 'a.xlsx');
      expect(rep.canCommit, isTrue);
      expect(a.calls.last.path, '/admin/payroll/structures/import');
      final done = await repo.structureImportCommit(
        bytes: [1, 2],
        filename: 'a.xlsx',
        fileSha256: 'a' * 64,
        reason: ' FY27 ',
        currentPassword: 'pw',
      );
      expect(done.created, 1);
      expect(a.calls.last.fields['reason'], 'FY27');
      expect(a.calls.last.fields['file_sha256'], 'a' * 64);
      expect(a.calls.last.fields['current_password'], 'pw');
    });

    test('409 errors surface their code', () async {
      final (repo, _) = _repo((c) => (409, {'error': {'code': 'INCREMENT_STALE', 'message': 'x', 'details': <Object?>[]}}));
      await expectLater(
        repo.approveIncrement('i1', expectedVersion: 1, currentPassword: 'pw'),
        throwsA(isA<ApiException>().having((e) => e.code, 'code', 'INCREMENT_STALE')),
      );
    });
  });
}
