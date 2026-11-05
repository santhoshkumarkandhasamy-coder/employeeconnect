import 'dart:convert';
import 'dart:io';

import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/admin/domain/admin_models.dart';
import 'package:employee_connect/features/leave/domain/leave_models.dart';
import 'package:employee_connect/features/payroll/domain/pay_items_models.dart';
import 'package:employee_connect/features/payroll/domain/payroll_models.dart';
import 'package:employee_connect/features/payroll/domain/payroll_money.dart';
import 'package:flutter_test/flutter_test.dart';

/// Models parse the HAND-WRITTEN contract fixtures
/// (`test/fixtures/payroll_phase3_contract_samples.json`, from api-contract.md
/// section 17; not captured from a backend).
void main() {
  late Map<String, dynamic> samples;
  setUpAll(() {
    samples = jsonDecode(File('test/fixtures/payroll_phase3_contract_samples.json').readAsStringSync())
        as Map<String, dynamic>;
  });

  List<Map<String, dynamic>> items(String key) =>
      ((samples[key] as Map<String, dynamic>)['items'] as List).cast<Map<String, dynamic>>();

  group('one-time items', () {
    test('parse status, flags, batch and run status', () {
      final list = items('items_page').map(OneTimeItem.fromJson).toList();
      expect(list, hasLength(3));
      expect(list[0].isPending, isTrue);
      expect(list[0].runStatus, 'processed');
      expect(runStatusHint(list[0].runStatus), 'Lands after the run is reopened');
      expect(list[1].isDeduction, isTrue);
      expect(list[1].flags, ['PERIOD_CLOSED']);
      expect(list[1].batchId, 'b1');
      expect(payItemFlagLabel('PERIOD_CLOSED'), contains('published'));
      expect(list[2].isPending, isFalse);
      expect(list[2].payslipId, 'p1');
      expect(runStatusHint('published'), 'Lands in the open revision');
      expect(runStatusHint('draft'), isNull);
      expect(runStatusHint(null), isNull);
    });

    test('ItemQuery is a value', () {
      expect(const ItemQuery(period: '2026-10'), const ItemQuery(period: '2026-10'));
      expect(const ItemQuery(period: '2026-10') == const ItemQuery(period: '2026-11'), isFalse);
      expect(const ItemQuery(period: '2026-10', code: 'BONUS').hashCode,
          const ItemQuery(period: '2026-10', code: 'BONUS').hashCode,);
    });

    test('creatable codes exclude system codes', () {
      expect(oneTimeCodes, ['BONUS', 'INCENTIVE', 'OTHER_EARNING', 'OTHER_DEDUCTION']);
      expect(oneTimeCodes, isNot(contains('OVERTIME')));
      expect(oneTimeCodes, isNot(contains('LEAVE_ENCASH')));
    });
  });

  group('import report', () {
    test('dry run with row errors cannot be committed', () {
      final r = ImportReport.fromJson(samples['import_dry_run'] as Map<String, dynamic>);
      expect(r.rows, 120);
      expect(r.valid, 118);
      expect(r.errors, hasLength(2));
      expect(r.errors.first.row, 14);
      expect(r.errors.first.field, 'employee_id');
      expect(importErrorLabel(r.errors.first.message), 'Employee ID not found');
      expect(r.byCode.first.code, 'BONUS');
      expect(r.byCode.first.total, '550000.00');
      expect(r.canCommit, isFalse);
    });

    test('a clean dry run can be committed', () {
      final r = ImportReport.fromJson(samples['import_dry_run_clean'] as Map<String, dynamic>);
      expect(r.canCommit, isTrue);
      expect(r.fileSha256, hasLength(64));
      expect(r.runStatus, 'processed');
    });

    test('an empty file or a missing hash cannot be committed', () {
      expect(
        ImportReport.fromJson({'period': '2026-11', 'file_sha256': 'a' * 64, 'rows': 0, 'valid': 0}).canCommit,
        isFalse,
      );
      expect(
        ImportReport.fromJson({'period': '2026-11', 'file_sha256': '', 'rows': 1, 'valid': 1}).canCommit,
        isFalse,
      );
    });

    test('commit failures put the row in the field text', () {
      final e = ImportRowError.fromJson({'field': 'row 14: employee_id', 'message': 'UNKNOWN_EMPLOYEE'});
      expect(e.row, 14);
      expect(e.field, 'employee_id');
      final f = ImportRowError.fromJson({'field': 'file', 'message': 'header'});
      expect(f.row, 0);
      expect(f.field, 'file');
    });

    test('every contract row error has a plain label', () {
      for (final c in [
        'UNKNOWN_EMPLOYEE', 'NOT_ELIGIBLE', 'SELF', 'INVALID_CODE', 'INVALID_LABEL',
        'INVALID_AMOUNT', 'FORMULA_NOT_ALLOWED', 'TOO_MANY_ITEMS', 'DUPLICATE_ROW',
      ]) {
        expect(importErrorLabel(c), isNot(c), reason: c);
      }
      expect(importErrorLabel('SOMETHING_NEW'), 'SOMETHING_NEW');
    });

    test('batches and cancel result', () {
      final b = ImportBatch.parseList(samples['batches'] as Map<String, dynamic>);
      expect(b, hasLength(2));
      expect(b.first.pendingCount, 110);
      expect(b.first.isCancelled, isFalse);
      expect(b.last.isCancelled, isTrue);
      final c = BatchCancelResult.fromJson(samples['batch_cancel'] as Map<String, dynamic>);
      expect((c.cancelled, c.notCancelled), (110, 8));
    });
  });

  group('loans', () {
    test('list rows carry next_installment, detail carries installments', () {
      final l = items('loan_list').map(Loan.fromJson).single;
      expect(l.reference, 'L-0007');
      expect(l.nextInstallment!.period, '2026-12');
      expect(l.installments, isEmpty);
      final d = Loan.fromJson(samples['loan_detail'] as Map<String, dynamic>);
      expect(d.installments, hasLength(5));
      expect(d.installments[2].status, 'processing');
      expect(d.installments[3].isPendingEmi, isTrue);
      expect(d.installments[4].flags, ['PERIOD_CLOSED']);
      expect(d.reason, 'Medical emergency');
      expect(d.version, 3);
      expect(d.isActive, isTrue);
      expect(d.kindLabel, 'Loan');
      expect(Loan.fromJson({'kind': 'advance'}).kindLabel, 'Advance');
    });

    test('create validation mirrors the contract limits', () {
      Map<String, String> v({
        String p = '30000',
        String e = '2500',
        String s = '2026-11',
        String r = 'Medical',
        int max = 60,
      }) =>
          validateLoanInput(principal: p, emi: e, startPeriod: s, reason: r, maxTenure: max);
      expect(v(), isEmpty);
      expect(v(p: '0.50')['principal'], isNotNull);
      expect(v(p: '1000000.01')['principal'], isNotNull);
      expect(v(p: '1000000')['principal'], isNull);
      expect(v(e: '0')['emi'], isNotNull);
      expect(v(e: '30000.01')['emi'], isNotNull);
      expect(v(e: '30000'), isEmpty);
      expect(v(p: '60000', e: '1000')['emi'], isNull);
      expect(v(p: '60001', e: '1000')['emi'], contains('61 instalments'));
      expect(v(p: '1200', e: '100', max: 6)['emi'], isNotNull);
      expect(v(s: 'x')['start_period'], isNotNull);
      expect(v(r: 'ab')['reason'], isNotNull);
      expect(v(r: 'x' * 201)['reason'], isNotNull);
      expect(v(p: 'abc')['principal'], isNotNull);
    });

    test('instalment count is ceil(principal / emi) in paise', () {
      expect(loanInstallmentCount('30000.00', '2500.00'), 12);
      expect(loanInstallmentCount('30000.00', '2600.00'), 12);
      expect(loanInstallmentCount('30000.00', '2300.00'), 14);
      expect(loanInstallmentCount('1.00', '0.30'), 4);
      expect(loanInstallmentCount('x', '1'), isNull);
      expect(loanInstallmentCount('100', '0'), isNull);
    });

    test('local schedule preview: equal instalments, remainder last', () {
      final s = previewLoanSchedule('10000', '3000', '2026-11');
      expect(s.map((e) => e.period), ['2026-11', '2026-12', '2027-01', '2027-02']);
      expect(s.map((e) => e.amount), ['3000.00', '3000.00', '3000.00', '1000.00']);
      expect(sumPaise(s.map((e) => e.amount)), 1000000);
      expect(previewLoanSchedule('', '3000', '2026-11'), isEmpty);
      expect(previewLoanSchedule('10000', '3000', 'bad'), isEmpty);
    });

    test('foreclosure reference format', () {
      expect(loanReferenceRe.hasMatch('UTR123/45-a b.c_d'), isTrue);
      expect(loanReferenceRe.hasMatch('bad;ref'), isFalse);
      expect(loanReferenceRe.hasMatch('x' * 61), isFalse);
    });

    test('employee view: no reason, no payslip ids, friendly installment states', () {
      final list = MyLoan.parseList(samples['me_loans'] as Map<String, dynamic>);
      expect(list, hasLength(2));
      expect(list.first.nextInstallment!.amount, '2500.00');
      expect(list.last.nextInstallment, isNull);
      expect(list.last.kindLabel, 'Advance');
      final d = MyLoan.fromJson(samples['me_loan'] as Map<String, dynamic>);
      expect(d.installments.map((i) => i.status), ['deducted', 'deferred', 'scheduled']);
      expect(d.installments.map((i) => myInstallmentLabels[i.status]), ['Deducted', 'Deferred', 'Scheduled']);
    });
  });

  group('arrears', () {
    test('proposal with month by month breakdown', () {
      final a = items('arrears_page').map(Arrears.fromJson).toList();
      expect(a[0].isProposed, isTrue);
      expect(a[0].isRecovery, isFalse);
      expect(a[0].breakdown, hasLength(3));
      expect(a[0].breakdown.first.diff, '1600.00');
      expect(a[0].breakdown.first.basicDiff, '1000.00');
      expect(a[0].breakdown.first.paidDays, 31.0);
      expect(a[0].triggerEffectiveFrom, '2026-08-01');
      expect(a[0].rangeLabel, 'August 2026 - October 2026');
      expect(a[0].version, 2);
      expect(a[0].canDismiss, isTrue);
    });

    test('a recovery keeps a positive amount and signed differences', () {
      final r = items('arrears_page').map(Arrears.fromJson).toList()[1];
      expect(r.isRecovery, isTrue);
      expect(r.amount, '1200.00');
      expect(r.breakdown.single.diff, '-1200.00');
      expect(r.rangeLabel, 'September 2026');
      expect(formatSignedRupees(r.breakdown.single.diff), '-₹1,200.00');
      expect(formatSignedRupees('1600.00'), '₹1,600.00');
    });

    test('an accepted row has its month and can still be dismissed', () {
      final p = items('arrears_page').map(Arrears.fromJson).toList()[2];
      expect(p.status, 'pending');
      expect(p.period, '2026-10');
      expect(p.runStatus, 'processed');
      expect(p.isProposed, isFalse);
      expect(p.canDismiss, isTrue);
      expect(Arrears.fromJson({'status': 'paid'}).canDismiss, isFalse);
    });
  });

  group('encashment', () {
    test('quote and list parse', () {
      final q = EncashmentQuote.fromJson(samples['encashment_quote'] as Map<String, dynamic>);
      expect(q.availableDays, 6.0);
      expect(q.days, 4.0);
      expect(q.dailyRate, '666.67');
      expect(q.amount, '2666.68');
      expect(q.leaveType.name, 'Earned Leave');
      expect(q.divisor, 30);
      final e = items('encashments_page').map(Encashment.fromJson).single;
      expect(e.isPending, isTrue);
      expect(e.leaveType.code, 'EL');
      expect(e.year, 2026);
    });

    test('days must be a positive 0.5 step within the available days', () {
      expect(validateEncashDays('4'), isNull);
      expect(validateEncashDays('4.5'), isNull);
      expect(validateEncashDays('0'), isNotNull);
      expect(validateEncashDays('-1'), isNotNull);
      expect(validateEncashDays('1.3'), 'Use steps of 0.5 day');
      expect(validateEncashDays('abc'), isNotNull);
      expect(validateEncashDays('7', max: 6), contains('6 days'));
      expect(validateEncashDays('6', max: 6), isNull);
    });
  });

  group('payslip components', () {
    test('admin inputs carry read-only components with source ids', () {
      final inputs = PayslipInputs.fromJson(samples['admin_payslip_inputs'] as Map<String, dynamic>);
      expect(inputs.components, hasLength(5));
      expect(inputs.adjustments, isEmpty);
      final emi = inputs.components.first;
      expect(emi.code, 'LOAN_EMI');
      expect(emi.sources.single.type, 'loan_installment');
      expect(componentSourceLabel(emi.sources.single.type), 'Loan instalment');
      expect(inputs.components.last.minutes, 390);
      expect(shortId(emi.sources.single.id), '9f1c2d3e');
      expect(shortId('abc'), 'abc');
    });

    test('older servers without components still parse', () {
      expect(PayslipInputs.fromJson({'tds': '1.00', 'adjustments': <Object?>[]}).components, isEmpty);
    });

    test('new warning codes have readable labels and old ones are kept', () {
      expect(payrollFlagLabel('ARREARS_PROPOSED'), contains('Arrears proposed'));
      expect(payrollFlagLabel('MANUAL_COMPONENT_OVERLAP'), contains('manual adjustment'));
      expect(payrollFlagLabel('OVERTIME_UNREVIEWED'), isNot('OVERTIME_UNREVIEWED'));
      expect(payrollFlagLabel('OVERTIME_CAPPED'), isNot('OVERTIME_CAPPED'));
      expect(payrollFlagLabel('NO_PAN'), 'No PAN');
    });
  });

  group('error messages (17.10)', () {
    String m(String code, {List<FieldError> details = const []}) =>
        payrollErrorMessage(ApiException(code: code, message: 'raw server text', details: details));

    test('every new code has its own text, never the raw server message', () {
      for (final c in [
        'PERIOD_LOCKED', 'ITEM_LOCKED', 'TOO_MANY_ITEMS', 'COMPONENTS_CHANGED', 'IMPORT_DUPLICATE',
        'IMPORT_FILE_CHANGED', 'LOAN_NOT_ACTIVE', 'LOAN_HAS_RECOVERIES', 'LOAN_LIMIT_REACHED',
        'ARREARS_STALE', 'NOT_PROPOSED', 'DUPLICATE_BILL', 'CLAIM_LIMIT_REACHED', 'OVERTIME_DISABLED',
        'IMPORT_INVALID', 'NOT_ENCASHABLE', 'INVALID_CATEGORY', 'AMOUNT_ABOVE_LIMIT', 'BILL_REQUIRED',
        'NOT_PENDING', 'INSUFFICIENT_BALANCE', 'REQUESTER_INACTIVE', 'STRUCTURE_MISSING',
        'FILE_TYPE_NOT_ALLOWED', 'FILE_TOO_LARGE',
      ]) {
        final t = m(c);
        expect(t, isNot('raw server text'), reason: c);
        expect(t, isNot(c), reason: c);
        expect(t.length, greaterThan(15), reason: c);
      }
    });

    test('actionable wording', () {
      expect(m('PERIOD_LOCKED'), contains('locked'));
      expect(m('ITEM_LOCKED'), contains('payslip'));
      expect(m('IMPORT_DUPLICATE'), contains('already imported'));
      expect(m('IMPORT_FILE_CHANGED'), contains('check again'));
      expect(m('ARREARS_STALE'), contains('Reload'));
      expect(m('NOT_ENCASHABLE'), contains('Encashable'));
      expect(m('LOAN_HAS_RECOVERIES'), contains('Foreclose'));
      expect(m('VERSION_CONFLICT'), contains('Reload'));
    });

    test('INSUFFICIENT_BALANCE names the available days from the details', () {
      expect(
        m('INSUFFICIENT_BALANCE', details: const [FieldError('available_days', '6.0')]),
        contains('6.0 days are available'),
      );
      expect(m('INSUFFICIENT_BALANCE'), isNot(contains('null')));
    });

    test('IMPORT_INVALID distinguishes a file-level problem from row errors', () {
      expect(
        m('IMPORT_INVALID', details: const [FieldError('file', 'header')]),
        contains('template'),
      );
      expect(
        m('IMPORT_INVALID', details: const [FieldError('row 3: amount', 'INVALID_AMOUNT')]),
        contains('rows listed'),
      );
    });

    test('INVALID_PERIOD no longer claims pay items cannot be in the future', () {
      expect(m('INVALID_PERIOD'), contains('12 months'));
    });
  });

  group('leave: encashed days and leave type fields', () {
    test('employee balance shows encashed days', () {
      final b = ((samples['leave_balance'] as Map<String, dynamic>)['leave'] as List)
          .map((e) => LeaveBalanceItem.fromJson(e as Map<String, dynamic>))
          .single;
      expect(b.encashed, 2.0);
      expect(b.remaining, 12.0);
      expect(LeaveBalanceItem.fromJson({'quota': 12}).encashed, 0);
    });

    test('admin balance row counts encashed days towards the minimum quota', () {
      final r = LeaveBalanceRow.fromJson({
        'leave_type': {'id': 't', 'name': 'Earned'},
        'quota': 18,
        'used': 3.0,
        'pending': 1.0,
        'encashed': 2.0,
      });
      expect(r.minQuota, 6.0);
      expect(r.withQuota(10).encashed, 2.0);
      expect(LeaveBalanceRow.fromJson({'leave_type_id': 't', 'used': 1.0}).minQuota, 1.0);
    });

    test('leave type form has the three encashment fields with contract ranges', () {
      FieldSpec f(String k) => leaveTypeSpec.fields.firstWhere((x) => x.key == k);
      expect(f('encashable').kind, FieldKind.boolean);
      expect(f('encashable').defaultValue, false);
      final cap = f('encash_max_days_per_year');
      expect(cap.nullable, isTrue);
      expect(validateField(cap, ''), isNull);
      expect(validateField(cap, '10'), isNull);
      expect(validateField(cap, '10.5'), isNull);
      expect(validateField(cap, '10.3'), 'Use steps of 0.5');
      expect(validateField(cap, '0'), isNotNull);
      final minBal = f('encash_min_balance');
      expect(validateField(minBal, '0'), isNull);
      expect(validateField(minBal, '-1'), isNotNull);
      expect(validateField(minBal, '2.25'), isNotNull);
      expect(validateField(minBal, ''), isNotNull);
    });

    test('the body carries the encashment values; an empty cap is null', () {
      final body = buildMasterBody(
        leaveTypeSpec,
        {
          'code': 'EL',
          'name': 'Earned',
          'annual_quota': '18',
          'requires_attachment_after_days': '',
          'max_consecutive_days': '',
          'encash_max_days_per_year': '',
          'encash_min_balance': '2',
        },
        {'is_paid': true, 'allow_half_day': true, 'is_active': true, 'encashable': true},
      );
      expect(body['encashable'], true);
      expect(body['encash_max_days_per_year'], isNull);
      expect(body.containsKey('encash_max_days_per_year'), isTrue);
      expect(body['encash_min_balance'], 2.0);
      final capped = buildMasterBody(
        leaveTypeSpec,
        {'code': 'EL', 'name': 'Earned', 'annual_quota': '18', 'encash_max_days_per_year': '7.5', 'encash_min_balance': '0'},
        {'encashable': false},
      );
      expect(capped['encash_max_days_per_year'], 7.5);
    });

    test('leave type list subtitle marks encashable types', () {
      final rec = (samples['leave_types_page'] as Map<String, dynamic>)['items'] as List;
      expect(leaveTypeSpec.subtitle(rec.first as Map<String, dynamic>), contains('encashable'));
      expect(leaveTypeSpec.subtitle(rec.last as Map<String, dynamic>), isNot(contains('encashable')));
    });
  });
}
