import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/payroll/domain/payroll_logic.dart';
import 'package:employee_connect/features/payroll/domain/payroll_models.dart';
import 'package:employee_connect/features/payroll/domain/phase5c_models.dart';
import 'package:employee_connect/features/payroll/domain/tds_computation.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/payroll_fakes.dart' show payslipJson;

/// api-contract.md 19.9 TdsComputation.
Map<String, dynamic> tdsJson({
  String mode = 'suggest',
  String suggested = '2500.00',
  String? applied,
  Map<String, dynamic>? override,
  List<String> flags = const [],
  String? shortfall,
  String regime = 'new',
  String regimeSource = 'declared',
  String totalTax = '30000.00',
}) =>
    {
      'fy': '2026-27',
      'mode': mode,
      'rules': {'id': 'ru1', 'version': 2},
      'regime': regime,
      'regime_source': regimeSource,
      'declaration_version': 3,
      'proof_basis': 'declared',
      'remaining_months': 6,
      'annual': {
        'salary_paid': '240000.00',
        'salary_this_month': '40000.00',
        'salary_projected': '200000.00',
        'previous_employer_income': '0.00',
        'other_income': '0.00',
        'exemptions': '0.00',
        'standard_deduction': '75000.00',
        'pt': '0.00',
        'chapter_via': '0.00',
        'taxable_income': '405000.00',
        'tax_on_income': '30000.00',
        'rebate': '0.00',
        'surcharge': '0.00',
        'cess': '0.00',
        'total_tax': totalTax,
      },
      'tds_paid_so_far': '6000.00',
      'previous_employer_tds': '0.00',
      'remaining_tax': '24000.00',
      'one_time_tax': '0.00',
      'suggested': suggested,
      'applied': applied,
      'override': override,
      'flags': flags,
      if (shortfall != null) 'shortfall': shortfall,
      'steps': {
        'sections': [
          {'section': '80C', 'declared_or_verified': '200000.00', 'allowed': '150000.00'},
        ],
        'hra_exemption': '0.00',
        'average_rate_pct': '7.40',
        'month_taxable': '40000.00',
        'recurring_total_tax': '30000.00',
      },
      'computed_at': '2026-10-25T10:00:00Z',
    };

Map<String, dynamic> adminTdsPayslip({
  String status = 'draft',
  Map<String, dynamic>? tds,
  List<String> warnings = const [],
  String tdsLine = '1200.00',
}) {
  final j = payslipJson(status: status, admin: true);
  (j['inputs'] as Map<String, dynamic>)['tds'] = tdsLine;
  j['warnings'] = warnings;
  j['tds_computation'] = tds;
  return j;
}

ApiException apiErr(String code, {int status = 409, List<FieldError> details = const []}) =>
    ApiException(code: code, message: 'raw server text', status: status, details: details);

void main() {
  group('TdsComputation parsing', () {
    test('reads the contract fields', () {
      final c = TdsComputation.fromJson(tdsJson(mode: 'apply', applied: '2500.00', flags: ['REGIME_DEFAULT']));
      expect(c.fy, '2026-27');
      expect(c.isApply, isTrue);
      expect(c.rulesVersion, 2);
      expect(c.regime, 'new');
      expect(c.regimeIsDefault, isFalse);
      expect(c.remainingMonths, 6);
      expect(c.annual.totalTax, '30000.00');
      expect(c.annual['standard_deduction'], '75000.00');
      expect(c.suggested, '2500.00');
      expect(c.applied, '2500.00');
      expect(c.override, isNull);
      expect(c.flags, ['REGIME_DEFAULT']);
      expect(c.steps!.sections.single.allowed, '150000.00');
      expect(c.steps!.averageRatePct, '7.40');
    });

    test('suggest mode has no applied figure; an override and a shortfall are read', () {
      final c = TdsComputation.fromJson(tdsJson(
        mode: 'apply',
        applied: '500.00',
        override: {'amount': '500.00', 'reason': 'Employee request'},
        shortfall: '2000.00',
      ),);
      expect(c.isOverridden, isTrue);
      expect(c.override!.reason, 'Employee request');
      expect(c.hasShortfall, isTrue);
      final s = TdsComputation.fromJson(tdsJson());
      expect(s.applied, isNull);
      expect(s.hasShortfall, isFalse);
      expect(s.isApply, isFalse);
    });

    test('a payslip without tds_computation (engine off) has none', () {
      expect(Payslip.fromJson(adminTdsPayslip()).tdsComputation, isNull);
      expect(Payslip.fromJson(adminTdsPayslip(tds: tdsJson())).tdsComputation!.suggested, '2500.00');
    });

    test('the employee projection and the preview share the result shape', () {
      final r = TdsResult.fromJson({
        'estimate': true,
        'computation': tdsJson(),
        'other_regime': {'regime': 'old', 'total_tax': '12500.00'},
      });
      expect(r.estimate, isTrue);
      expect(r.otherRegime!.regime, 'old');
      expect(TdsResult.fromJson({'computation': tdsJson()}).otherRegime, isNull);
    });

    test('flags have a chip text and a sentence; unknown ones fall back to the code', () {
      for (final f in [
        'TDS_EXCESS',
        'TDS_CAPPED_NET',
        'TDS_OVERRIDDEN',
        'TAX_RULES_NOT_VERIFIED',
        'RULES_CHANGED',
        'NO_PAN_206AA',
        'PERQUISITE_REVIEW',
        'LEAVE_ENCASH_REVIEW',
        'REGIME_DEFAULT',
        'NO_DECLARATION',
        'TDS_SHORTFALL_FINAL',
        'TDS_MANUAL_REPLACED',
        'PREV_EMPLOYER_UNVERIFIED',
        'RULES_SUPERSEDED',
      ]) {
        expect(tdsFlagLabel(f), isNot(f), reason: f);
        expect(tdsFlagExplanation(f), isNot(f), reason: f);
      }
      expect(tdsFlagLabel('SOMETHING_NEW'), 'SOMETHING_NEW');
      // the run review and the payslip chips use the same texts
      expect(payrollFlagLabel('TDS_CAPPED_NET'), tdsFlagLabel('TDS_CAPPED_NET'));
      expect(payrollFlagLabel('RULES_CHANGED'), tdsFlagLabel('RULES_CHANGED'));
      expect(payrollBlockHint('RULES_CHANGED'), contains('Refresh the run'));
      expect(payrollBlockHint('TDS_SHORTFALL_FINAL'), contains('TDS override'));
      expect(payrollBlockHint('NO_PAN'), isNull);
      final blocked = payrollErrorMessage(
        const ApiException(
          code: 'PAYROLL_BLOCKED',
          message: 'x',
          details: [FieldError('EMP001', 'RULES_CHANGED'), FieldError('EMP002', 'TDS_SHORTFALL_FINAL')],
        ),
      );
      expect(blocked, allOf(contains('EMP001'), contains('Refresh the run'), contains('EMP002'), contains('TDS override')));
    });
  });

  group('payslip patch (override reason)', () {
    Payslip slip(Map<String, dynamic>? tds) => Payslip.fromJson(adminTdsPayslip(tds: tds));

    test('suggest mode: a changed TDS is sent without a reason', () {
      final p = slip(tdsJson());
      final patch = buildPayslipPatch(p, const PayslipEditInput(tds: '2500', tdsReason: 'ignored'));
      expect(patch['tds'], '2500.00');
      expect(patch.containsKey('tds_override_reason'), isFalse);
    });

    test('apply mode: a changed TDS is sent with the trimmed reason', () {
      final p = slip(tdsJson(mode: 'apply', applied: '1200.00'));
      final patch = buildPayslipPatch(p, const PayslipEditInput(tds: '900', tdsReason: '  Employee request  '));
      expect(patch['tds'], '900.00');
      expect(patch['tds_override_reason'], 'Employee request');
    });

    test('apply mode: an unchanged TDS sends neither', () {
      final p = slip(tdsJson(mode: 'apply', applied: '1200.00'));
      final patch = buildPayslipPatch(p, const PayslipEditInput(tds: '1200.00', tdsReason: 'x'));
      expect(patchHasChanges(patch), isFalse);
    });

    test('engine off: behaves as before 5c, never a reason', () {
      final p = slip(null);
      final patch = buildPayslipPatch(p, const PayslipEditInput(tds: '999', tdsReason: 'x'));
      expect(patch['tds'], '999.00');
      expect(patch.containsKey('tds_override_reason'), isFalse);
    });

    test('handing back to the engine sends tds: null, only for an overridden line', () {
      final over = slip(tdsJson(mode: 'apply', applied: '500.00', override: {'amount': '500.00', 'reason': 'r r'}));
      final patch = buildPayslipPatch(over, const PayslipEditInput(tds: '1200.00', tdsHandBack: true));
      expect(patch.containsKey('tds'), isTrue);
      expect(patch['tds'], isNull);
      expect(patch.containsKey('tds_override_reason'), isFalse);
      // no override: the flag is ignored
      final plain = slip(tdsJson(mode: 'apply', applied: '1200.00'));
      expect(patchHasChanges(buildPayslipPatch(plain, const PayslipEditInput(tds: '1200.00', tdsHandBack: true))), isFalse);
    });

    test('tdsReasonRequired follows the computation mode', () {
      expect(tdsReasonRequired(slip(null)), isFalse);
      expect(tdsReasonRequired(slip(tdsJson())), isFalse);
      expect(tdsReasonRequired(slip(tdsJson(mode: 'apply', applied: '1.00'))), isTrue);
    });
  });

  group('validators', () {
    test('override reason is 3 to 200 characters', () {
      expect(validateTdsOverrideReason(''), isNotNull);
      expect(validateTdsOverrideReason('ab'), 'At least 3 characters');
      expect(validateTdsOverrideReason('abc'), isNull);
      expect(validateTdsOverrideReason('a' * 201), 'At most 200 characters');
      expect(validateTdsOverrideReason('', required: false), isNull);
    });

    test('BSR code is exactly 7 digits', () {
      expect(validateBsrCode(''), isNotNull);
      expect(validateBsrCode('123456'), isNotNull);
      expect(validateBsrCode('12345678'), isNotNull);
      expect(validateBsrCode('12a4567'), isNotNull);
      expect(validateBsrCode('0510016'), isNull);
    });

    test('challan serial is 1 to 5 digits', () {
      expect(validateChallanSerial(''), isNotNull);
      expect(validateChallanSerial('123456'), isNotNull);
      expect(validateChallanSerial('x1'), isNotNull);
      expect(validateChallanSerial('1'), isNull);
      expect(validateChallanSerial('00123'), isNull);
    });

    test('deposit date: valid, not in the future, not before the month starts', () {
      final today = DateTime(2026, 10, 25);
      final start = DateTime(2026, 10);
      expect(validateDepositedOn('nope', today, start), isNotNull);
      expect(validateDepositedOn('2026-10-26', today, start), 'Not in the future');
      expect(validateDepositedOn('2026-09-30', today, start), contains('Not before'));
      expect(validateDepositedOn('2026-10-01', today, start), isNull);
      expect(validateDepositedOn('2026-10-25', today, start), isNull);
    });

    test('preview month must be inside the financial year', () {
      expect(validateTaxPeriod('', '2026-27'), isNull);
      expect(validateTaxPeriod('2026-04', '2026-27'), isNull);
      expect(validateTaxPeriod('2027-03', '2026-27'), isNull);
      expect(validateTaxPeriod('2027-04', '2026-27'), isNotNull);
      expect(validateTaxPeriod('2026-03', '2026-27'), isNotNull);
      expect(validateTaxPeriod('26-4', '2026-27'), isNotNull);
    });

    test('quarters: 1 = Apr to Jun ... 4 = Jan to Mar', () {
      expect(quarterOf(DateTime(2026, 4, 1)), 1);
      expect(quarterOf(DateTime(2026, 6, 30)), 1);
      expect(quarterOf(DateTime(2026, 7)), 2);
      expect(quarterOf(DateTime(2026, 11, 6)), 3);
      expect(quarterOf(DateTime(2027, 1)), 4);
      expect(quarterOf(DateTime(2027, 3, 31)), 4);
      expect(quarterLabel(4), contains('Jan to Mar'));
    });
  });

  group('regime comparison', () {
    TdsComputation comp(String total, {String regime = 'new'}) =>
        TdsComputation.fromJson(tdsJson(totalTax: total, regime: regime));

    test('the cheaper regime and the difference', () {
      final a = compareRegimes(comp('30000.00'), const OtherRegime(regime: 'old', totalTax: '12500.50'))!;
      expect(a.cheaper, 'other');
      expect(a.differencePaise, 1749950);
      expect(paiseText(a.differencePaise), '17499.50');
      final b = compareRegimes(comp('10000.00'), const OtherRegime(regime: 'old', totalTax: '12500.00'))!;
      expect(b.cheaper, 'chosen');
      final c = compareRegimes(comp('10000.00'), const OtherRegime(regime: 'old', totalTax: '10000.00'))!;
      expect(c.cheaper, isNull);
      expect(compareRegimes(comp('1.00'), null), isNull);
    });
  });

  group('summary', () {
    test('items, totals of the whole set and paging', () {
      final p = TdsSummaryPage.fromJson({
        'items': [
          {
            'user': {'id': 'u1', 'employee_id': 'EMP001', 'full_name': 'Asha Rao'},
            'regime': 'new',
            'total_tax': '30000.00',
            'tds_paid_so_far': '6000.00',
            'remaining_tax': '24000.00',
            'suggested_this_month': '2500.00',
            'manual_this_month': '1200.00',
            'flags': ['TDS_OVERRIDDEN'],
          },
        ],
        'page': 1,
        'page_size': 20,
        'total': 45,
        'totals': {'total_tax': '900000.00', 'tds_paid_so_far': '100000.00', 'remaining_tax': '800000.00'},
      });
      expect(p.items.single.user.display, 'Asha Rao (EMP001)');
      expect(p.items.single.differs, isTrue);
      expect(p.totalTax, '900000.00');
      expect(p.remainingTax, '800000.00');
      expect(p.hasMore, isTrue);
    });

    test('query equality', () {
      expect(const TdsSummaryQuery(fy: 'a', q: 'b'), const TdsSummaryQuery(fy: 'a', q: 'b'));
      expect(const TdsSummaryQuery(fy: 'a'), isNot(const TdsSummaryQuery(fy: 'a', flag: 'TDS_EXCESS')));
    });
  });

  group('challan details on the dues', () {
    test('masked BSR, serial and deposit date are read as the API returns them', () {
      final i = StatutoryDueItem.fromJson({
        'type': 'tds',
        'amount': '5000.00',
        'employees': 2,
        'status': 'paid',
        'paid_on': '2026-10-07',
        'reference': 'CH1',
        'bsr_code': 'XXX3456',
        'challan_serial': '00042',
        'deposited_on': '2026-10-06',
      });
      expect(i.bsrCode, 'XXX3456');
      expect(i.challanSerial, '00042');
      expect(i.depositedOn, '2026-10-06');
      final none = StatutoryDueItem.fromJson({'type': 'pf', 'amount': '1.00', 'employees': 1, 'status': 'unpaid'});
      expect(none.bsrCode, isNull);
    });
  });

  group('settings', () {
    test('the five TDS keys are in the form specs', () {
      final keys = payrollSettingSpecs.map((s) => s.key).toSet();
      for (final k in tdsSettingKeys) {
        expect(keys, contains(k));
      }
      final month = payrollSettingSpecs.firstWhere((s) => s.key == tdsProofsFromMonthKey);
      expect(validatePayrollNumber('0', month), isNotNull);
      expect(validatePayrollNumber('13', month), isNotNull);
      expect(validatePayrollNumber('4.5', month), 'Enter a whole number');
      expect(validatePayrollNumber('4', month), isNull);
      final mode = payrollSettingSpecs.firstWhere((s) => s.key == tdsEngineModeKey);
      expect(mode.options.map((o) => o.$1), ['suggest', 'apply']);
    });
  });

  group('plain-language errors', () {
    test('TDS_ENGINE_OFF and TAX_RULES_NOT_VERIFIED never show the raw server text', () {
      final off = phase5cErrorMessage(apiErr('TDS_ENGINE_OFF'));
      expect(off, contains('switched off'));
      expect(off, isNot(contains('raw server text')));
      expect(phase5cErrorMessage(apiErr('TAX_RULES_NOT_VERIFIED')), contains('not verified'));
    });

    test('override reason field error', () {
      final e = apiErr('VALIDATION_ERROR', status: 422, details: const [FieldError('tds_override_reason', 'x')]);
      expect(phase5cErrorMessage(e), contains('Say why the TDS is changed by hand'));
      expect(payslipEditorErrorMessage(e), contains('Say why'));
    });

    test('challan fields', () {
      ApiException v(String f) => apiErr('VALIDATION_ERROR', status: 422, details: [FieldError(f, 'm')]);
      expect(phase5cErrorMessage(v('bsr_code')), contains('7 digits'));
      expect(phase5cErrorMessage(v('challan_serial')), contains('1 to 5 digits'));
      expect(phase5cErrorMessage(v('deposited_on')), contains('date'));
    });

    test('24Q with no published month, wrong password, invalid period', () {
      expect(tax24qErrorMessage(apiErr('RUN_NOT_PUBLISHED')), contains('quarter'));
      expect(phase5cErrorMessage(apiErr('REAUTH_FAILED', status: 403)), 'Your password was not correct.');
      expect(phase5cErrorMessage(apiErr('INVALID_PERIOD', status: 422)), contains('financial year'));
    });

    test('the editor still explains the 5a codes', () {
      expect(payslipEditorErrorMessage(apiErr('PUBLISH_REQUEST_REQUIRED')), contains('second person'));
    });

    test('employee wording does not point to admin screens', () {
      expect(myProjectionMessage(apiErr('TDS_ENGINE_OFF')), isNot(contains('Settings')));
      expect(myProjectionMessage(apiErr('TAX_RULES_NOT_VERIFIED')), contains('estimate'));
      expect(tdsUnavailable(apiErr('TDS_ENGINE_OFF')), isTrue);
      expect(tdsUnavailable(apiErr('SERVER_ERROR', status: 500)), isFalse);
    });
  });
}
