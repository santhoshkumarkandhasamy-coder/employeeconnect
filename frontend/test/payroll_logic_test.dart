import 'dart:io';
import 'dart:typed_data';

import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/admin/domain/admin_models.dart';
import 'package:employee_connect/features/payroll/data/payroll_delivery.dart';
import 'package:employee_connect/features/payroll/data/payroll_repository.dart';
import 'package:employee_connect/features/payroll/domain/payroll_logic.dart';
import 'package:employee_connect/features/payroll/domain/payroll_models.dart';
import 'package:employee_connect/features/payroll/domain/payroll_money.dart';
import 'package:employee_connect/console/console_shell.dart';
import 'package:employee_connect/features/food/data/food_repository.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

Payslip _payslip({
  int version = 3,
  double? override,
  String? reason,
  String? tds = '1200.00',
  String? pt,
  List<Adjustment> adj = const [],
  bool hold = false,
  String? note,
}) =>
    Payslip.fromJson({
      'id': 'p1',
      'run_id': 'r1',
      'period': '2026-10',
      'revision': 1,
      'status': 'draft',
      'hold': hold,
      'version': version,
      'company': {'name': 'Aksan'},
      'employee': {'employee_id': 'EMP001', 'full_name': 'A B', 'payment_mode': 'cash'},
      'days': {'basis_days': 31.0, 'employed_days': 31.0, 'lop_days': 1.5, 'paid_days': 29.5},
      'user': {'id': 'u1', 'employee_id': 'EMP001', 'full_name': 'A B'},
      'lop': {
        'auto': 1.5,
        'override': override,
        'override_reason': reason,
        'breakdown': {'unpaid_leave': 1.0, 'half_day_attendance': 0.5},
      },
      'inputs': {
        'tds': tds,
        'pt_override': pt,
        'adjustments': [for (final a in adj) a.toJson()],
        'note': note,
      },
      'earnings': <Object?>[],
      'deductions': <Object?>[],
      'errors': <Object?>[],
      'warnings': <Object?>[],
    });

void main() {
  group('money', () {
    test('parses user input to paise and rejects bad text', () {
      expect(parseMoneyPaise('25000'), 2500000);
      expect(parseMoneyPaise('25000.5'), 2500050);
      expect(parseMoneyPaise('0.07'), 7);
      expect(parseMoneyPaise('10000000.00'), 1000000000);
      expect(parseMoneyPaise('10000000.01'), isNull);
      expect(parseMoneyPaise('1.234'), isNull);
      expect(parseMoneyPaise('-5'), isNull);
      expect(parseMoneyPaise('abc'), isNull);
      expect(parseMoneyPaise(''), isNull);
      expect(parseMoneyPaise('123456789'), isNull);
    });

    test('normalises to two decimals as a string, never a float', () {
      expect(normalizeMoney('25000'), '25000.00');
      expect(normalizeMoney('1.5'), '1.50');
      expect(normalizeMoney('x'), isNull);
      expect(paiseToMoney(7), '0.07');
    });

    test('Indian grouping (lakh / crore)', () {
      expect(groupIndian('999'), '999');
      expect(groupIndian('1000'), '1,000');
      expect(groupIndian('100000'), '1,00,000');
      expect(groupIndian('1234567'), '12,34,567');
      expect(groupIndian('10000000'), '1,00,00,000');
      expect(formatInr('1234567.5'), '12,34,567.50');
      expect(formatInr('0'), '0.00');
      expect(formatInr(null), '-');
      expect(formatRupees('35300.00'), '₹35,300.00');
    });

    test('sums in paise without float drift', () {
      expect(paiseToMoney(sumPaise(['0.10', '0.20'])), '0.30');
    });
  });

  group('validators', () {
    test('money field', () {
      expect(validateMoney('100'), isNull);
      expect(validateMoney(''), isNotNull);
      expect(validateMoney('', required: false), isNull);
      expect(validateMoney('0', positive: true), isNotNull);
      expect(validateMoney('1.234'), isNotNull);
    });

    test('codes: format, reserved words', () {
      expect(validatePayrollCode('SPECIAL'), isNull);
      expect(validatePayrollCode('special'), isNull); // upper-cased on send
      expect(validatePayrollCode('A'), isNotNull);
      expect(validatePayrollCode('1ABC'), isNotNull);
      expect(validatePayrollCode('BASIC'), contains('reserved'));
      expect(validatePayrollCode('LOAN_EMI'), contains('reserved'));
      expect(validatePayrollCode('ABCDEFGHIJKLMNOPQ'), isNotNull);
      expect(validatePayrollLabel('x' * 41), isNotNull);
    });

    test('structure lists: unique codes and limits', () {
      const a = AllowanceLine(code: 'SPECIAL', name: 'S', amount: '1.00');
      const d = FixedDeductionLine(code: 'CANTEEN', name: 'C', amount: '1.00');
      expect(validateStructureLists([a], [d]), isNull);
      expect(validateStructureLists([a, a], []), contains('twice'));
      expect(
        validateStructureLists([a], [const FixedDeductionLine(code: 'special', name: 'x', amount: '1')]),
        contains('twice'),
      );
      expect(
        validateStructureLists([for (var i = 0; i < 16; i++) AllowanceLine(code: 'A$i', name: 'n', amount: '1')], []),
        contains('15'),
      );
    });

    test('LOP override: half steps, bounded by employed days, reason required', () {
      expect(validateLopOverride('', 31), isNull);
      expect(validateLopOverride('1.5', 31), isNull);
      expect(validateLopOverride('1.3', 31), contains('half-day'));
      expect(validateLopOverride('32', 31), contains('At most'));
      expect(validateLopOverride('-1', 31), isNotNull);
      expect(validateLopReason('ab', overrideSet: true), isNotNull);
      expect(validateLopReason('Forgot to check in', overrideSet: true), isNull);
      expect(validateLopReason('', overrideSet: false), isNull);
    });

    test('identifier formats', () {
      expect(validatePan('abcde1234f'), isNull);
      expect(validatePan('ABCDE12345'), isNotNull);
      expect(validatePan(''), isNull);
      expect(validateIfsc('HDFC0001234'), isNull);
      expect(validateIfsc('HDFC1001234'), isNotNull);
      expect(validateBankAccount('123456789'), isNull);
      expect(validateBankAccount('12345678'), isNotNull);
      expect(validateUan('100200300400'), isNull);
      expect(validateUan('1002003004'), isNotNull);
      expect(validateEsiIp('1234567890'), isNull);
      expect(validateEsiIp('12345'), isNotNull);
      expect(cleanIdentifier(' ab 12 '), 'AB12');
    });
  });

  group('payslip patch (only changed keys)', () {
    test('no edits -> only expected_version', () {
      final p = _payslip();
      final patch = buildPayslipPatch(
        p,
        const PayslipEditInput(tds: '1200', note: ''),
      );
      expect(patch, {'expected_version': 3});
      expect(patchHasChanges(patch), isFalse);
    });

    test('LOP override sends value and reason; clearing sends null', () {
      final p = _payslip();
      var patch = buildPayslipPatch(
        p,
        const PayslipEditInput(tds: '1200.00', lopOverride: 1, lopReason: ' Confirmed present '),
      );
      expect(patch['lop_override'], 1);
      expect(patch['lop_override_reason'], 'Confirmed present');

      final withOverride = _payslip(override: 1, reason: 'Confirmed present');
      patch = buildPayslipPatch(withOverride, const PayslipEditInput(tds: '1200.00'));
      expect(patch.containsKey('lop_override'), isTrue);
      expect(patch['lop_override'], isNull);
      expect(patch.containsKey('lop_override_reason'), isFalse);
    });

    test('tds, pt override, adjustments, hold and note', () {
      final p = _payslip();
      final patch = buildPayslipPatch(
        p,
        const PayslipEditInput(
          tds: '1500',
          ptOverride: '200',
          adjustments: [Adjustment(code: 'ARREARS', label: ' Sept arrears ', amount: '2500')],
          hold: true,
          note: ' check ',
        ),
      );
      expect(patch['tds'], '1500.00'); // string, 2 decimals
      expect(patch['pt_override'], '200.00');
      expect(patch['adjustments'], [
        {'code': 'ARREARS', 'label': 'Sept arrears', 'amount': '2500.00'},
      ]);
      expect(patch['hold'], isTrue);
      expect(patch['note'], 'check');
      expect(patch['expected_version'], 3);
    });

    test('clearing the PT override sends null', () {
      final p = _payslip(pt: '200.00');
      final patch = buildPayslipPatch(p, const PayslipEditInput(tds: '1200.00'));
      expect(patch.containsKey('pt_override'), isTrue);
      expect(patch['pt_override'], isNull);
    });
  });

  group('details patch', () {
    PayrollDetails details({bool revealed = false}) => PayrollDetails(
          userId: 'u1',
          designation: 'Analyst',
          paymentMode: 'bank_transfer',
          includeInPayroll: true,
          revealed: revealed,
          bankName: 'HDFC',
          bankAccount: revealed ? '123456789012' : 'XXXXXX9012',
          pan: revealed ? 'ABCDE1234F' : 'XXXXX1234F',
        );

    Map<String, Object?> patch(
      PayrollDetails d, {
      String designation = 'Analyst',
      String mode = 'bank_transfer',
      bool include = true,
      String bankName = 'HDFC',
      String account = '',
      String pan = '',
    }) =>
        buildDetailsPatch(
          original: d,
          designation: designation,
          paymentMode: mode,
          include: include,
          bankName: bankName,
          bankAccount: account,
          bankIfsc: '',
          pan: pan,
          uan: '',
          esiIp: '',
        );

    test('masked fields are not sent unless typed', () {
      expect(patch(details()), isEmpty);
      expect(patch(details(), pan: 'abcde9999z'), {'pan': 'ABCDE9999Z'});
      expect(patch(details(), designation: 'Senior Analyst'), {'designation': 'Senior Analyst'});
      expect(patch(details(), mode: 'cash', include: false), {
        'payment_mode': 'cash',
        'include_in_payroll': false,
      });
    });

    test('revealed fields: unchanged -> nothing, emptied -> null', () {
      final d = details(revealed: true);
      expect(patch(d, account: '123456789012', pan: 'ABCDE1234F'), isEmpty);
      expect(patch(d, account: '123456789012'), {'pan': null});
    });
  });

  group('settings and PT slabs', () {
    test('number validation and parsing', () {
      const rate = PayrollSettingSpec('k', 'Rate', PayrollSettingKind.number, 'g', min: 0, max: 20);
      expect(validatePayrollNumber('12', rate), isNull);
      expect(validatePayrollNumber('0.75', rate), isNull);
      expect(validatePayrollNumber('21', rate), contains('At most'));
      expect(validatePayrollNumber('1.234', rate), contains('2 decimals'));
      expect(validatePayrollNumber('', rate), isNotNull);
      expect(parsePayrollNumber('12'), isA<int>());
      expect(parsePayrollNumber('0.75'), 0.75);
    });

    test('only changed settings are sent (12 equals 12.0)', () {
      final changes = changedPayrollSettings(
        {'payroll.pf_employee_rate_pct': 12, 'payroll.rounding': 'rupee', 'payroll.mask_pan': true},
        {'payroll.pf_employee_rate_pct': 12.0, 'payroll.rounding': 'paise', 'payroll.mask_pan': true},
      );
      expect(changes, {'payroll.rounding': 'paise'});
    });

    test('slab validation: range, amount, months, overlap', () {
      const ok = [
        PtSlab(minGross: '0.00', maxGross: '21000.00', amount: '0.00', months: [3, 9]),
        PtSlab(minGross: '21001.00', maxGross: '30000.00', amount: '135.00', months: [3, 9]),
        PtSlab(minGross: '30001.00', amount: '315.00', months: [3, 9]),
      ];
      expect(validatePtSlabs(ok), isNull);
      expect(validatePtSlabs(const []), isNull);
      expect(
        validatePtSlabs(const [PtSlab(minGross: '5000.00', maxGross: '1000.00', amount: '1.00')]),
        contains('maximum'),
      );
      expect(validatePtSlabs(const [PtSlab(minGross: '', amount: '1.00')]), contains('minimum'));
      expect(
        validatePtSlabs(const [PtSlab(minGross: '1.00', amount: 'x')]),
        contains('tax amount'),
      );
      expect(
        validatePtSlabs(const [
          PtSlab(minGross: '0.00', maxGross: '30000.00', amount: '1.00', months: [3]),
          PtSlab(minGross: '20000.00', amount: '2.00', months: [3, 9]),
        ]),
        contains('overlap'),
      );
      // Same range in different months is fine.
      expect(
        validatePtSlabs(const [
          PtSlab(minGross: '0.00', amount: '1.00', months: [3]),
          PtSlab(minGross: '0.00', amount: '2.00', months: [9]),
        ]),
        isNull,
      );
      // A month-specific slab may sit on an all-months slab (it wins).
      expect(
        validatePtSlabs(const [
          PtSlab(minGross: '25000.00', amount: '200.00'),
          PtSlab(minGross: '25000.00', amount: '300.00', months: [2]),
        ]),
        isNull,
      );
      expect(
        validatePtSlabs([for (var i = 0; i < 21; i++) PtSlab(minGross: '${i * 10}.00', maxGross: '${i * 10}.00', amount: '1.00')]),
        contains('20'),
      );
    });
  });

  group('models', () {
    test('payslip list items carry no amounts', () {
      final items = PayslipListItem.parseList({
        'year': 2026,
        'items': [
          {
            'id': 'a',
            'period': '2026-10',
            'revision': 2,
            'published_at': '2026-10-31T09:00:00Z',
            'revised': true,
            'pay_date': '2026-10-31',
          },
        ],
      });
      expect(items.single.revised, isTrue);
      expect(items.single.period, '2026-10');
      expect(periodTitle(items.single.period), 'October 2026');
    });

    test('payslip detail keeps money as strings', () {
      final p = Payslip.fromJson({
        'id': 'a',
        'period': '2026-10',
        'revision': 2,
        'status': 'published',
        'company': {'name': 'Aksan', 'address': null},
        'employee': {'employee_id': 'EMP001', 'full_name': 'A B', 'payment_mode': 'bank_transfer'},
        'days': {'basis_days': 31.0, 'employed_days': 31.0, 'lop_days': 1.5, 'paid_days': 29.5},
        'earnings': [
          {'code': 'BASIC', 'label': 'Basic', 'monthly': '20000.00', 'amount': '19032.00'},
          {'code': 'BONUS', 'label': 'Diwali', 'monthly': null, 'amount': '5000.00'},
        ],
        'deductions': [
          {'code': 'PF', 'label': 'Provident Fund', 'amount': '1800.00'},
        ],
        'employer_contributions': null,
        'totals': {
          'gross_earnings': '38500.00',
          'total_deductions': '3200.00',
          'net_pay': '35300.00',
          'net_pay_words': 'Rupees Thirty-Five Thousand Three Hundred Only',
        },
        'revision_info': {'reason': 'Late leave', 'supersedes_id': 's1', 'superseded_by_id': null},
      });
      expect(p.earnings.first.amount, '19032.00');
      expect(p.earnings.last.monthly, isNull);
      expect(p.employerContributions, isNull);
      expect(p.totals!.netPay, '35300.00');
      expect(p.revisionInfo!.supersedesId, 's1');
      expect(p.isAdminShape, isFalse);
    });

    test('admin payslip parses lop, inputs, flags', () {
      final p = _payslip(override: 1, reason: 'why', adj: const [Adjustment(code: 'BONUS', label: 'b', amount: '1.00')]);
      expect(p.isAdminShape, isTrue);
      expect(p.lop!.override, 1);
      expect(p.lop!.halfDayAttendance, 0.5);
      expect(p.inputs!.adjustments.single.code, 'BONUS');
      expect(p.user!.id, 'u1');
    });

    test('run and rows parse; draft rows with errors have null money', () {
      final row = RunPayslipRow.fromJson({
        'id': 'p',
        'user': {'id': 'u', 'employee_id': 'EMP014', 'full_name': 'X'},
        'revision': 1,
        'status': 'draft',
        'hold': false,
        'paid_days': 0,
        'lop_days': 0,
        'gross_earnings': null,
        'total_deductions': null,
        'net_pay': null,
        'errors': ['STRUCTURE_MISSING'],
        'warnings': <Object?>[],
      });
      expect(row.netPay, isNull);
      expect(row.errors, ['STRUCTURE_MISSING']);
      final run = PayrollRun.fromJson({
        'id': 'r',
        'period': '2026-10',
        'status': 'processed',
        'counts': {'employees': 48, 'draft': 2, 'processed': 45, 'errors': 0},
        'totals': {'gross_earnings': '1850000.00', 'net_pay': '1690000.00'},
      });
      expect(run.isProcessed, isTrue);
      expect(run.counts.processed, 45);
      expect(run.totals.netPay, '1690000.00');
    });

    test('structure input computes gross and serialises money as strings', () {
      const input = StructureInput(
        effectiveFrom: '2026-04-01',
        basic: '20000.00',
        hra: '8000.00',
        allowances: [AllowanceLine(code: 'SPECIAL', name: 'Special', amount: '10000.00')],
        fixedDeductions: [],
        tdsMonthly: '1200.00',
        pfApplicable: true,
        pfOnFullBasic: false,
        esiApplicable: true,
        ptApplicable: true,
      );
      expect(input.monthlyGross, '38000.00');
      final j = input.toJson();
      expect(j['basic'], '20000.00');
      expect((j['allowances']! as List).single, containsPair('amount', '10000.00'));
      expect(j['effective_from'], '2026-04-01');
      expect(structureEffectiveFrom(2026, 4), '2026-04-01');
    });
  });

  group('permissions and error mapping', () {
    test('permission helpers', () {
      expect(canViewOwnPayslips(['payroll.view_own']), isTrue);
      expect(canViewOwnPayslips(['payroll.view_all']), isFalse);
      expect(canOpenPayroll(['payroll.view_all']), isTrue);
      expect(canManagePayroll(['payroll.view_all']), isFalse);
      expect(canPublishPayroll(['payroll.publish']), isTrue);
    });

    test('admin home and console show Payroll only with payroll.view_all', () {
      expect(adminSections(['payroll.view_all']), contains(AdminSection.payroll));
      expect(adminSections(['payroll.view_own', 'payroll.manage']), isNot(contains(AdminSection.payroll)));
      expect(
        visibleDestinations(['console.access', 'payroll.view_all']).map((d) => d.label),
        contains('Payroll'),
      );
      expect(
        visibleDestinations(['console.access']).map((d) => d.label),
        isNot(contains('Payroll')),
      );
    });

    test('domain error codes map to clear messages', () {
      String m(String code, {List<FieldError> details = const []}) =>
          payrollErrorMessage(ApiException(code: code, message: 'x', details: details));
      expect(m('PAYROLL_RUN_EXISTS'), contains('already exists'));
      expect(m('PAYROLL_LOCKED'), contains('locked'));
      expect(m('STRUCTURE_IN_USE'), contains('new structure'));
      expect(m('VERSION_CONFLICT'), contains('Reload'));
      expect(m('NOTHING_TO_PUBLISH'), contains('nothing'));
      expect(m('SECOND_PERSON_REQUIRED'), contains('different person'));
      expect(
        m('PAYROLL_BLOCKED', details: const [FieldError('EMP014', 'STRUCTURE_MISSING')]),
        allOf(contains('EMP014'), contains('No salary structure')),
      );
    });

    test('file names from Content-Disposition are sanitised', () {
      expect(
        safePayrollFileName('attachment; filename="payslip-EMP001-2026-10.pdf"', 'x.pdf', '.pdf'),
        'payslip-EMP001-2026-10.pdf',
      );
      expect(safePayrollFileName('attachment; filename="../../etc/passwd"', 'x.pdf', '.pdf'), 'passwd.pdf');
      expect(safePayrollFileName(null, 'x.pdf', '.pdf'), 'x.pdf');
      expect(safePayrollFileName('attachment; filename="r"', 'x.xlsx', '.xlsx'), 'r.xlsx');
    });
  });

  group('privacy', () {
    test('payroll code never touches the JSON cache', () {
      final files = Directory('lib/features/payroll')
          .listSync(recursive: true)
          .whereType<File>()
          .where((f) => f.path.endsWith('.dart'));
      expect(files, isNotEmpty);
      for (final f in files) {
        final src = f
            .readAsLinesSync()
            .where((l) => !l.trimLeft().startsWith('//'))
            .join(' ');
        expect(src.contains('json_cache'), isFalse, reason: f.path);
        expect(src.contains('jsonCacheProvider'), isFalse, reason: f.path);
      }
    });

    test('every payroll screen is wrapped in SecureScreen', () {
      for (final name in [
        'payslips_screen',
        'payslip_detail_screen',
        'payroll_home_screen',
        'run_review_screen',
        'payslip_edit_screen',
        'employee_payroll_screen',
        'payroll_details_screen',
        'structure_form_screen',
      ]) {
        final src = File('lib/features/payroll/presentation/$name.dart').readAsStringSync();
        expect(src.contains('SecureScreen('), isTrue, reason: name);
      }
    });
  });

  group('StoreFileDelivery', () {
    Future<(BuildContext, Directory)> setup(WidgetTester tester) async {
      late BuildContext ctx;
      await tester.pumpWidget(
        MaterialApp(home: Builder(builder: (c) {
          ctx = c;
          return const SizedBox();
        },),),
      );
      final dir = Directory.systemTemp.createTempSync('payroll_delivery_test');
      addTearDown(() {
        if (dir.existsSync()) dir.deleteSync(recursive: true);
      });
      return (ctx, dir);
    }

    testWidgets('saves privately, hands to the share sheet, then deletes the file', (tester) async {
      final (ctx, dir) = await setup(tester);
      final opener = _Opener();
      final container = ProviderContainer();
      addTearDown(container.dispose);
      final delivery = StoreFileDelivery(
        store: ExportStore(Directory('${dir.path}/ec_exports')),
        opener: opener,
        confirmed: container.read(shareConfirmedProvider.notifier)..state = true,
      );
      await tester.runAsync(() async {
        final r = await delivery.deliver(ctx, PayrollFile(_bytes, 'payslip.pdf'));
        expect(r, DeliveryOutcome.done);
      });
      expect(opener.path, endsWith('payslip.pdf'));
      expect(opener.existedWhenOpened, isTrue);
      expect(File(opener.path!).existsSync(), isFalse);
    });

    testWidgets('file is deleted even when sharing is dismissed or unavailable', (tester) async {
      final (ctx, dir) = await setup(tester);
      for (final result in [OpenResult.dismissed, OpenResult.unavailable]) {
        final opener = _Opener()..result = result;
        final container = ProviderContainer();
        addTearDown(container.dispose);
        final delivery = StoreFileDelivery(
          store: ExportStore(Directory('${dir.path}/ec_exports')),
          opener: opener,
          confirmed: container.read(shareConfirmedProvider.notifier)..state = true,
        );
        await tester.runAsync(() async {
          final r = await delivery.deliver(ctx, PayrollFile(_bytes, 'p.pdf'));
          expect(
            r,
            result == OpenResult.dismissed ? DeliveryOutcome.cancelled : DeliveryOutcome.unavailable,
          );
        });
        expect(File(opener.path!).existsSync(), isFalse);
      }
    });
  });
}

final _bytes = Uint8List.fromList([1, 2, 3]);

class _Opener implements FileOpener {
  String? path;
  bool existedWhenOpened = false;
  OpenResult result = OpenResult.handedOver;
  @override
  Future<OpenResult> open(String path) async {
    this.path = path;
    existedWhenOpened = File(path).existsSync();
    return result;
  }
}
