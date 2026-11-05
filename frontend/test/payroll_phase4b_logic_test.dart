import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/payroll/domain/payroll_models.dart';
import 'package:employee_connect/features/payroll/domain/phase4b_models.dart';
import 'package:flutter_test/flutter_test.dart';

Map<String, dynamic> fnfJson({
  String status = 'draft',
  List<String> warnings = const [],
  Map<String, dynamic>? summary,
  String createdBy = 'maker',
  String? submittedBy,
}) =>
    {
      'id': 'f1',
      'user': {'id': 'u1', 'employee_id': 'EMP001', 'full_name': 'Asha Rao'},
      'status': status,
      'resignation_date': '2026-11-15',
      'last_working_day': '2026-12-15',
      'exit_reason': 'resignation',
      'settlement_period': '2026-12',
      'run_status': 'none',
      'notice': {
        'required_days': 30,
        'served_days': 31,
        'shortfall_days': 0,
        'computed_shortfall_days': 0,
        'waived': false,
        'override': null,
        'basis': 'gross',
        'divisor': 30,
        'daily_rate': '1333.33',
        'amount': '0.00',
      },
      'encashments': [
        {
          'leave_type': {'id': 'lt1', 'code': 'EL', 'name': 'Earned leave'},
          'year': 2026,
          'days': 6.0,
          'available_days': 8.0,
          'daily_rate': '666.67',
          'amount': '4000.02',
        },
      ],
      'loans': [
        {
          'loan': {'id': 'l1', 'reference': 'L-0007', 'kind': 'loan'},
          'outstanding': '22500.00',
          'mode': null,
        },
      ],
      'items': [
        {'code': 'BONUS', 'label': 'Pro-rata bonus', 'amount': '3000.00'},
      ],
      'move_open_items': true,
      'open_items': <Object?>[],
      'gratuity': null,
      'warnings': warnings,
      'summary': summary,
      'final_payslip': null,
      'remarks': null,
      'created_by': {'id': createdBy, 'employee_id': 'EMP009', 'full_name': 'Maker'},
      'submitted_by': submittedBy == null ? null : {'id': submittedBy, 'employee_id': 'EMP010', 'full_name': 'Sub'},
      'version': 3,
    };

void main() {
  group('parsing', () {
    test('an F&F keeps money as strings and reads nested parts', () {
      final f = Fnf.fromJson(fnfJson());
      expect(f.notice.dailyRate, '1333.33');
      expect(f.notice.servedDays, 31);
      expect(f.encashments.single.amount, '4000.02');
      expect(f.encashments.single.days, 6.0);
      expect(f.loans.single.mode, isNull);
      expect(f.hasUnresolvedLoan, isTrue);
      expect(f.items.single.code, 'BONUS');
      expect(f.version, 3);
      expect(f.isEditable, isTrue);
    });

    test('served_days may be null without a resignation date', () {
      final j = fnfJson();
      (j['notice'] as Map<String, dynamic>)['served_days'] = null;
      expect(Fnf.fromJson(j).notice.servedDays, isNull);
    });

    test('approved and settled are read-only', () {
      for (final s in ['approved', 'settled', 'submitted', 'cancelled']) {
        final f = Fnf.fromJson(fnfJson(status: s));
        expect(f.isEditable, isFalse, reason: s);
        expect(f.isReadOnly, isTrue, reason: s);
      }
      expect(Fnf.fromJson(fnfJson()).isReadOnly, isFalse);
    });

    test('/me/fnf: null and a settlement', () {
      expect(MyFnf.parse({'fnf': null}), isNull);
      final m = MyFnf.parse({
        'fnf': {
          'id': 'f1',
          'status': 'settled',
          'last_working_day': '2026-12-15',
          'settlement_period': '2026-12',
          'final_payslip_id': 'p9',
        },
      })!;
      expect(m.isSettled, isTrue);
      expect(m.finalPayslipId, 'p9');
    });

    test('payslip is_final, final_settlement and details notice period', () {
      final item = PayslipListItem.fromJson({'id': 'p', 'period': '2026-12', 'revision': 1, 'is_final': true});
      expect(item.isFinal, isTrue);
      final d = PayrollDetails.fromJson({
        'user_id': 'u',
        'notice_period_days': 45,
        'last_working_day': '2026-12-15',
      });
      expect(d.noticePeriodDays, 45);
      expect(d.lastWorkingDay, '2026-12-15');
      expect(PayrollDetails.fromJson({'user_id': 'u', 'notice_period_days': null}).noticePeriodDays, isNull);
    });
  });

  group('maker-checker (cosmetic)', () {
    test('the employee, creator, submitter, last editor and contributors cannot decide', () {
      final f = Fnf.fromJson({
        ...fnfJson(submittedBy: 'sub'),
        'last_edited_by': {'id': 'editor', 'employee_id': 'E', 'full_name': 'E'},
        'contributors': ['contrib'],
      });
      for (final who in ['u1', 'maker', 'sub', 'editor', 'contrib', null, '']) {
        expect(canDecideFnf(f, who), isFalse, reason: '$who');
      }
      expect(canDecideFnf(f, 'checker'), isTrue);
      expect(cannotDecideFnfReason(f, 'u1'), contains('own settlement'));
      expect(cannotDecideFnfReason(f, 'maker'), contains('different person'));
      expect(cannotDecideFnfReason(f, 'checker'), isNull);
    });
  });

  group('negative settlement (Q5)', () {
    test('detected from warnings, preview or snapshot', () {
      expect(fnfNetNegative(Fnf.fromJson(fnfJson())), isFalse);
      expect(fnfNetNegative(Fnf.fromJson(fnfJson(warnings: ['NEGATIVE_NET_ESTIMATE']))), isTrue);
      final p = FnfPreview.fromJson({
        'estimate': true,
        'totals': {'gross_earnings': '100.00', 'total_deductions': '300.00', 'net_pay': '-200.00'},
      });
      expect(p.isNegative, isTrue);
      expect(fnfNetNegative(Fnf.fromJson(fnfJson()), p), isTrue);
      expect(isNegativeMoney('-0.00'), isFalse);
      expect(isNegativeMoney('0.00'), isFalse);
    });
  });

  group('exit validation', () {
    final today = DateTime(2026, 11, 5);
    ExitValues v({DateTime? lwd, DateTime? res, String? period, int? notice}) => ExitValues(
          lastWorkingDay: lwd,
          resignationDate: res,
          exitReason: 'resignation',
          settlementPeriod: period,
          noticePeriodDays: notice,
        );

    test('valid', () {
      expect(validateExit(v(lwd: DateTime(2026, 12, 15), res: DateTime(2026, 11, 15), period: '2026-12'), today), isEmpty);
    });

    test('LWD is required and at most 180 days ahead', () {
      expect(validateExit(v(period: '2026-12'), today).keys, contains('last_working_day'));
      expect(validateExit(v(lwd: DateTime(2027, 6, 1), period: '2027-06'), today).keys, contains('last_working_day'));
    });

    test('resignation cannot be after the LWD', () {
      expect(
        validateExit(v(lwd: DateTime(2026, 12, 15), res: DateTime(2026, 12, 20), period: '2026-12'), today).keys,
        contains('resignation_date'),
      );
    });

    test('settlement month from the LWD month to 3 months later', () {
      final lwd = DateTime(2026, 12, 15);
      expect(validateExit(v(lwd: lwd, period: '2026-11'), today).keys, contains('settlement_period'));
      expect(validateExit(v(lwd: lwd, period: '2027-03'), today), isEmpty);
      expect(validateExit(v(lwd: lwd, period: '2027-04'), today).keys, contains('settlement_period'));
    });

    test('notice days 0..365', () {
      expect(validateExit(v(lwd: DateTime(2026, 12, 15), period: '2026-12', notice: 400), today).keys,
          contains('notice_period_days'),);
    });

    test('create body uses wire formats', () {
      final b = fnfCreateBody(v(lwd: DateTime(2026, 12, 15), res: DateTime(2026, 11, 15), period: '2026-12', notice: 30));
      expect(b['last_working_day'], '2026-12-15');
      expect(b['resignation_date'], '2026-11-15');
      expect(b['notice_period_days'], 30);
      expect(b['remarks'], isNull);
    });
  });

  group('edit body', () {
    test('lists are sent whole, override carries its reason, money stays a string', () {
      final body = const FnfEdit(
        waived: false,
        overrideDays: 5,
        overrideReason: ' Handover completed early ',
        encashments: [FnfEncashmentInput(leaveTypeId: 'lt1', year: 2026, days: 2.5)],
        loans: [
          FnfLoanInput(loanId: 'l1', mode: 'outside', paidOn: '2026-12-10', reference: 'UTR123'),
          FnfLoanInput(loanId: 'l2', mode: 'payroll'),
        ],
        items: [FnfItemInput(code: 'BONUS', label: 'Bonus', amount: '3000.00')],
        moveOpenItems: true,
      ).toJson(4);
      expect(body['expected_version'], 4);
      expect(body['notice'], {
        'waived': false,
        'override': {'days': 5, 'reason': 'Handover completed early'},
      });
      expect(body['encashments'], [
        {'leave_type_id': 'lt1', 'year': 2026, 'days': 2.5},
      ]);
      expect((body['loans']! as List)[0], {'loan_id': 'l1', 'mode': 'outside', 'paid_on': '2026-12-10', 'reference': 'UTR123'});
      expect((body['loans']! as List)[1], {'loan_id': 'l2', 'mode': 'payroll'});
      expect(body['items'], [
        {'code': 'BONUS', 'label': 'Bonus', 'amount': '3000.00'},
      ]);
      expect(body.containsKey('notice_period_days'), isFalse);
    });

    test('clearing the override sends null', () {
      final body = const FnfEdit(waived: true, clearOverride: true).toJson(2);
      expect((body['notice']! as Map)['override'], isNull);
      expect((body['notice']! as Map)['waived'], isTrue);
    });
  });

  group('field validation', () {
    test('encashment days in steps of 0.5', () {
      expect(validateEncashDays('2'), isNull);
      expect(validateEncashDays('2.5'), isNull);
      expect(validateEncashDays('2.3'), isNotNull);
      expect(validateEncashDays('0'), isNotNull);
      expect(validateEncashDays('abc'), isNotNull);
      expect(validateEncashDays('5', available: 4), isNotNull);
    });

    test('reasons 3..200 and payment references', () {
      expect(validateFnfReason(''), isNotNull);
      expect(validateFnfReason('ab'), isNotNull);
      expect(validateFnfReason('Handover done'), isNull);
      expect(validateFnfReason('x' * 201), isNotNull);
      expect(validatePaymentReference('UTR123'), isNull);
      expect(validatePaymentReference(''), isNotNull);
      expect(validatePaymentReference('a;b'), isNotNull);
    });
  });

  group('error messages', () {
    String msg(String code, {List<FieldError> details = const []}) =>
        fnfErrorMessage(ApiException(code: code, message: 'x', status: 409, details: details));

    test('new codes have plain-language text', () {
      expect(msg('FNF_EXISTS'), contains('already has a final settlement'));
      expect(msg('FNF_OPEN'), contains('in progress'));
      expect(msg('FNF_MANAGED'), contains('belongs to a final settlement'));
      expect(msg('FNF_NEGATIVE'), contains('owe the company'));
      expect(msg('FNF_NOT_REVOCABLE'), contains('cannot be revoked'));
      expect(msg('FNF_NOT_INCLUDED'), contains('Reopen the payroll run'));
      expect(msg('LOAN_UNRESOLVED'), contains('active loan'));
      expect(msg('SECOND_PERSON_REQUIRED'), contains('different person'));
      expect(msg('VERSION_CONFLICT'), contains('Someone else changed'));
      expect(msg('NOT_SUBMITTED'), contains('no longer waiting'));
    });

    test('details name the failing part', () {
      expect(
        msg('ITEM_LOCKED', details: const [FieldError('loans[1]', 'x')]),
        contains('Check loan 2'),
      );
      expect(fnfPartLabel('encashments[0]'), 'leave encashment line 1');
      expect(fnfPartLabel('items[2]'), 'other item 3');
      expect(fnfPartLabel('notice'), 'the notice shortfall');
    });

    test('payrollErrorMessage also knows them', () {
      expect(payrollErrorMessage(const ApiException(code: 'FNF_NOT_INCLUDED', message: 'x')), contains('process it again'));
    });

    test('warning labels and stale codes', () {
      expect(fnfWarningLabel('CLAIMS_OPEN'), contains('claims'));
      expect(payrollFlagLabel('FNF_SETTLEMENT_ONLY'), contains('Settlement only'));
      expect(payrollFlagLabel('EXIT_IN_MONTH'), contains('leaves'));
      expect(isStaleFnfError(const ApiException(code: 'VERSION_CONFLICT', message: 'x')), isTrue);
      expect(isStaleFnfError(const ApiException(code: 'VALIDATION_ERROR', message: 'x')), isFalse);
    });
  });
}
