import 'dart:typed_data';

import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/security/screen_protect.dart';
import 'package:employee_connect/features/payroll/data/payroll_repository.dart';
import 'package:employee_connect/features/payroll/domain/payroll_models.dart';
import 'package:employee_connect/features/payroll/domain/phase5b_models.dart' show MyTaxYear;
import 'package:employee_connect/features/payroll/domain/phase5c_models.dart';
import 'package:employee_connect/features/payroll/domain/tds_computation.dart';
import 'package:employee_connect/features/payroll/presentation/my_tax_screen.dart';
import 'package:employee_connect/features/payroll/presentation/payroll_settings_tab.dart';
import 'package:employee_connect/features/payroll/presentation/payslip_edit_screen.dart';
import 'package:employee_connect/features/payroll/presentation/statutory_tab.dart';
import 'package:employee_connect/features/payroll/presentation/tds_section.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/payroll_fakes.dart';
import 'payroll_phase5c_logic_test.dart' show adminTdsPayslip, apiErr, tdsJson;

class Fake5c extends FakeRepo {
  // engine
  TdsSummaryPage summary = TdsSummaryPage.fromJson({
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
    'total': 1,
    'totals': {'total_tax': '30000.00', 'tds_paid_so_far': '6000.00', 'remaining_tax': '24000.00'},
  });
  ApiException? engineError;
  final summaryCalls = <(TdsSummaryQuery, int)>[];
  final previewCalls = <Map<String, Object?>>[];
  final statementCalls = <Map<String, Object?>>[];
  final q24Calls = <Map<String, Object?>>[];
  ApiException? q24Error;
  int wrongPw = 0;
  Map<String, dynamic> other = {'regime': 'old', 'total_tax': '12500.00'};
  Map<String, dynamic> comp = tdsJson();
  ApiException? projectionError;

  @override
  Future<List<MyTaxYear>> myTaxYears() async => const [];

  @override
  Future<TdsSummaryPage> tdsSummary(TdsSummaryQuery q, {int page = 1}) async {
    summaryCalls.add((q, page));
    if (engineError != null) throw engineError!;
    return summary;
  }

  @override
  Future<TdsResult> tdsPreview({required String userId, String? fy, String? period, String? regime}) async {
    previewCalls.add({'user': userId, 'fy': fy, 'period': period, 'regime': regime});
    if (engineError != null) throw engineError!;
    return TdsResult.fromJson({'computation': comp, 'other_regime': other});
  }

  void _pw(String pw) {
    if (wrongPw > 0) {
      wrongPw--;
      throw const ApiException(code: 'REAUTH_FAILED', message: 'no', status: 403);
    }
  }

  @override
  Future<PayrollFile> taxStatement(String userId, {required String fy, required String currentPassword}) async {
    statementCalls.add({'user': userId, 'fy': fy, 'pw': currentPassword});
    _pw(currentPassword);
    if (engineError != null) throw engineError!;
    return PayrollFile(Uint8List.fromList([37, 80, 68, 70]), 'tax-computation-EMP001-2026-27.pdf');
  }

  @override
  Future<PayrollFile> tax24q({required String fy, required int quarter, required String currentPassword}) async {
    q24Calls.add({'fy': fy, 'quarter': quarter, 'pw': currentPassword});
    _pw(currentPassword);
    if (q24Error != null) throw q24Error!;
    return PayrollFile(Uint8List.fromList([1]), '24q-$fy-q$quarter.xlsx');
  }

  @override
  Future<TdsResult> myTaxProjection({String? fy}) async {
    if (projectionError != null) throw projectionError!;
    return TdsResult.fromJson({'estimate': true, 'computation': comp, 'other_regime': other});
  }
}

const _manage = ['payroll.view_all', 'payroll.manage', 'payroll.publish'];
const _view = ['payroll.view_all'];

Future<void> _tap(WidgetTester tester, String key) async {
  final f = find.byKey(ValueKey(key));
  await tester.ensureVisible(f);
  await tester.pumpAndSettle();
  await tester.tap(f);
  await tester.pumpAndSettle();
}

Future<void> _enter(WidgetTester tester, String key, String text) async {
  final f = find.byKey(ValueKey(key));
  await tester.ensureVisible(f);
  await tester.pumpAndSettle();
  await tester.enterText(f, text);
  await tester.pumpAndSettle();
}

Future<void> _password(WidgetTester tester, String pw) async {
  expect(find.byKey(const ValueKey('reauth-password')), findsOneWidget);
  await tester.enterText(find.byKey(const ValueKey('reauth-password')), pw);
  await tester.tap(find.text('Confirm'));
  await tester.pumpAndSettle();
}

void main() {
  group('payslip editor: TDS card', () {
    Future<Fake5c> open(
      WidgetTester tester, {
      Map<String, dynamic>? tds,
      String status = 'draft',
      List<String> warnings = const [],
      List<String> errors = const [],
      String tdsLine = '1200.00',
      List<String> perms = _manage,
    }) async {
      final j = adminTdsPayslip(status: status, tds: tds, warnings: warnings, tdsLine: tdsLine)..['errors'] = errors;
      final repo = Fake5c()..adminPayslip = Payslip.fromJson(j);
      await pumpPayroll(tester, const PayslipEditScreen(payslipId: 'p1'), repo: repo, perms: perms);
      return repo;
    }

    testWidgets('suggest mode shows the computed figure beside the manual one, with the flag explained', (tester) async {
      await open(tester, tds: tdsJson(flags: ['TDS_EXCESS']), warnings: ['TDS_EXCESS']);
      expect(find.byKey(const ValueKey('tds-card')), findsOneWidget);
      expect(find.text('Suggestion only'), findsOneWidget);
      expect(find.descendant(of: find.byKey(const ValueKey('tds-suggested')), matching: find.text('₹2,500.00')), findsOneWidget);
      expect(find.descendant(of: find.byKey(const ValueKey('tds-manual')), matching: find.text('₹1,200.00')), findsOneWidget);
      expect(find.byKey(const ValueKey('tds-flag-TDS_EXCESS')), findsOneWidget);
      expect(find.textContaining('Nothing is refunded'), findsOneWidget);
      // the chip on top uses the same plain text
      expect(find.text('TDS already more than the tax due'), findsWidgets);
    });

    testWidgets('suggest mode: no reason is asked, and the computed figure can be copied in', (tester) async {
      final repo = await open(tester, tds: tdsJson());
      await _tap(tester, 'tds-use-suggested');
      expect(find.widgetWithText(TextFormField, '2500.00'), findsOneWidget);
      expect(find.byKey(const ValueKey('tds-override-reason')), findsNothing);
      await _tap(tester, 'save-payslip');
      expect(repo.savedPatches.single, {'expected_version': 3, 'tds': '2500.00'});
    });

    testWidgets('apply mode: changing the TDS needs a reason, which is sent with it', (tester) async {
      final repo = await open(tester, tds: tdsJson(mode: 'apply', applied: '1200.00'));
      expect(find.byKey(const ValueKey('tds-override-reason')), findsNothing);
      await _enter(tester, 'tds-field', '900');
      expect(find.byKey(const ValueKey('tds-override-reason')), findsOneWidget);
      await _tap(tester, 'save-payslip');
      expect(find.textContaining('Say why the TDS is changed by hand'), findsWidgets);
      expect(repo.savedPatches, isEmpty);
      await _enter(tester, 'tds-override-reason', 'ab');
      await _tap(tester, 'save-payslip');
      expect(find.text('At least 3 characters'), findsOneWidget);
      await _enter(tester, 'tds-override-reason', 'Employee request');
      await _tap(tester, 'save-payslip');
      expect(repo.savedPatches.single, {
        'expected_version': 3,
        'tds': '900.00',
        'tds_override_reason': 'Employee request',
      });
    });

    testWidgets('the server asking for the reason is explained in plain words', (tester) async {
      final repo = await open(tester, tds: tdsJson(mode: 'apply', applied: '1200.00'));
      repo.saveError = apiErr(
        'VALIDATION_ERROR',
        status: 422,
        details: const [FieldError('tds_override_reason', 'required')],
      );
      await _enter(tester, 'tds-field', '900');
      await _enter(tester, 'tds-override-reason', 'Employee request');
      await _tap(tester, 'save-payslip');
      expect(find.textContaining('The payroll run is applying the computed TDS'), findsOneWidget);
    });

    testWidgets('an override is shown with its reason and can be handed back to the engine', (tester) async {
      final repo = await open(
        tester,
        tds: tdsJson(
          mode: 'apply',
          applied: '500.00',
          override: {'amount': '500.00', 'reason': 'Employee request'},
          flags: ['TDS_OVERRIDDEN'],
        ),
        warnings: ['TDS_OVERRIDDEN'],
        tdsLine: '500.00',
      );
      expect(find.byKey(const ValueKey('tds-override-note')), findsOneWidget);
      expect(find.textContaining('Employee request'), findsWidgets);
      await _tap(tester, 'tds-hand-back');
      expect(find.byKey(const ValueKey('tds-hand-back-note')), findsOneWidget);
      expect(find.byKey(const ValueKey('tds-field')), findsNothing);
      await _tap(tester, 'save-payslip');
      expect(repo.savedPatches.single.containsKey('tds'), isTrue);
      expect(repo.savedPatches.single['tds'], isNull);
      expect(repo.savedPatches.single.containsKey('tds_override_reason'), isFalse);
    });

    testWidgets('keeping my figure after choosing hand-back restores the field', (tester) async {
      await open(
        tester,
        tds: tdsJson(mode: 'apply', applied: '500.00', override: {'amount': '500.00', 'reason': 'Because'}),
        tdsLine: '500.00',
      );
      await _tap(tester, 'tds-hand-back');
      await _tap(tester, 'tds-keep-mine');
      expect(find.byKey(const ValueKey('tds-field')), findsOneWidget);
    });

    testWidgets('capped to the net pay shows the shortfall that is carried on', (tester) async {
      await open(
        tester,
        tds: tdsJson(mode: 'apply', applied: '1200.00', suggested: '3000.00', shortfall: '1800.00', flags: ['TDS_CAPPED_NET']),
        warnings: ['TDS_CAPPED_NET'],
      );
      expect(find.byKey(const ValueKey('tds-shortfall')), findsOneWidget);
      expect(find.textContaining('₹1,800.00'), findsWidgets);
      expect(find.textContaining('next months pick it up'), findsWidgets);
    });

    testWidgets('rules not verified: manual TDS, with an explanation and no figures', (tester) async {
      await open(tester, warnings: ['TAX_RULES_NOT_VERIFIED']);
      expect(find.byKey(const ValueKey('tds-card')), findsOneWidget);
      expect(find.byKey(const ValueKey('tds-suggested')), findsNothing);
      expect(find.textContaining('The manual TDS figure is used'), findsOneWidget);
    });

    testWidgets('rules changed is offered as a warning to refresh the run', (tester) async {
      await open(tester, tds: tdsJson(flags: ['RULES_CHANGED']), warnings: ['RULES_CHANGED']);
      expect(find.textContaining('refresh the draft run'), findsOneWidget);
    });

    testWidgets('blocking errors RULES_CHANGED and TDS_SHORTFALL_FINAL come with a plain hint', (tester) async {
      await open(
        tester,
        tds: tdsJson(mode: 'apply', applied: '1000.00', flags: ['TDS_SHORTFALL_FINAL']),
        errors: ['RULES_CHANGED', 'TDS_SHORTFALL_FINAL'],
        warnings: ['TDS_MANUAL_REPLACED', 'PREV_EMPLOYER_UNVERIFIED', 'RULES_SUPERSEDED'],
      );
      expect(find.byKey(const ValueKey('tds-flag-RULES_CHANGED')), findsOneWidget);
      expect(find.byKey(const ValueKey('tds-flag-TDS_SHORTFALL_FINAL')), findsOneWidget);
      expect(find.textContaining('set a TDS override with a reason'), findsOneWidget);
      expect(find.byKey(const ValueKey('tds-flag-TDS_MANUAL_REPLACED')), findsOneWidget);
      expect(find.byKey(const ValueKey('tds-flag-PREV_EMPLOYER_UNVERIFIED')), findsOneWidget);
      expect(find.byKey(const ValueKey('tds-flag-RULES_SUPERSEDED')), findsOneWidget);
    });

    testWidgets('engine off: no TDS card at all', (tester) async {
      await open(tester);
      expect(find.byKey(const ValueKey('tds-card')), findsNothing);
    });

    testWidgets('a processed payslip shows the frozen computation, read-only', (tester) async {
      await open(tester, status: 'processed', tds: tdsJson(mode: 'apply', applied: '2500.00'));
      expect(find.byKey(const ValueKey('tds-card')), findsOneWidget);
      expect(find.byKey(const ValueKey('tds-applied')), findsOneWidget);
      expect(find.byKey(const ValueKey('tds-field')), findsNothing);
      expect(find.byKey(const ValueKey('tds-use-suggested')), findsNothing);
      expect(find.byKey(const ValueKey('tds-hand-back')), findsNothing);
    });

    testWidgets('the details open to the annual figures', (tester) async {
      await open(tester, tds: tdsJson());
      await _tap(tester, 'tds-details');
      expect(find.byKey(const ValueKey('tds-annual-taxable_income')), findsOneWidget);
      expect(find.text('₹4,05,000.00'), findsOneWidget);
      expect(find.textContaining('6 months left in 2026-27'), findsOneWidget);
      expect(find.text('80C'), findsOneWidget);
    });

    testWidgets('the editor is screenshot-protected', (tester) async {
      await open(tester, tds: tdsJson());
      expect(find.byType(SecureScreen), findsWidgets);
    });
  });

  group('TDS summary and exports', () {
    Future<Fake5c> openSummary(WidgetTester tester, {Fake5c? repo, FakeDelivery? delivery}) async {
      final r = repo ?? Fake5c();
      await pumpPayroll(tester, const Scaffold(body: TdsSection()), repo: r, perms: _view, delivery: delivery);
      return r;
    }

    testWidgets('lists employees with totals of the whole set and both figures for the month', (tester) async {
      await openSummary(tester);
      expect(find.byKey(const ValueKey('tds-totals')), findsOneWidget);
      expect(find.text('Totals for 1 employee in FY 2026-27'), findsOneWidget);
      expect(find.text('Asha Rao (EMP001)'), findsOneWidget);
      expect(find.text('This month: computed'), findsOneWidget);
      expect(find.text('Different from the computed figure'), findsOneWidget);
      expect(find.text('Set by hand'), findsWidgets);
    });

    testWidgets('a flag filter reloads the list', (tester) async {
      final repo = await openSummary(tester);
      await _tap(tester, 'tds-filter-TDS_EXCESS');
      expect(repo.summaryCalls.last.$1.flag, 'TDS_EXCESS');
      expect(repo.summaryCalls.last.$2, 1);
    });

    testWidgets('empty list', (tester) async {
      final repo = Fake5c()
        ..summary = TdsSummaryPage.fromJson({'items': <Object?>[], 'page': 1, 'page_size': 20, 'total': 0});
      await openSummary(tester, repo: repo);
      expect(find.text('No employees match.'), findsOneWidget);
    });

    testWidgets('engine off is explained, not shown as a failure', (tester) async {
      final repo = Fake5c()..engineError = apiErr('TDS_ENGINE_OFF');
      await openSummary(tester, repo: repo);
      expect(find.textContaining('switched off'), findsOneWidget);
      expect(find.text('Try again'), findsNothing);
    });

    testWidgets('a server failure has a retry', (tester) async {
      final repo = Fake5c()..engineError = apiErr('SERVER_ERROR', status: 500);
      await openSummary(tester, repo: repo);
      expect(find.text('Try again'), findsOneWidget);
    });

    testWidgets('rules not verified is explained', (tester) async {
      final repo = Fake5c()..engineError = apiErr('TAX_RULES_NOT_VERIFIED');
      await openSummary(tester, repo: repo);
      expect(find.textContaining('not verified'), findsOneWidget);
    });

    testWidgets('24Q asks for year, quarter and the password (a wrong one is asked again)', (tester) async {
      final delivery = FakeDelivery();
      final repo = Fake5c()..wrongPw = 1;
      await openSummary(tester, repo: repo, delivery: delivery);
      await _tap(tester, 'tds-24q');
      expect(find.text('24Q working file'), findsOneWidget);
      // today in the tests is 25 Oct 2026: quarter 3
      expect(find.text('Quarter 3 (Oct to Dec)'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('q24-quarter')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Quarter 4 (Jan to Mar)').last);
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('q24-continue')));
      await tester.pumpAndSettle();
      await _password(tester, 'wrong');
      expect(find.text('That password is not correct.'), findsOneWidget);
      await _password(tester, 'right');
      expect(repo.q24Calls.map((c) => c['pw']), ['wrong', 'right']);
      expect(repo.q24Calls.last['fy'], '2026-27');
      expect(repo.q24Calls.last['quarter'], 4);
      expect(delivery.files.single.filename, '24q-2026-27-q4.xlsx');
    });

    testWidgets('cancelling the password sends nothing', (tester) async {
      final repo = Fake5c();
      await openSummary(tester, repo: repo);
      await _tap(tester, 'tds-24q');
      await tester.tap(find.byKey(const ValueKey('q24-continue')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();
      expect(repo.q24Calls, isEmpty);
    });

    testWidgets('24Q for a quarter with no published month says so', (tester) async {
      final repo = Fake5c()..q24Error = apiErr('RUN_NOT_PUBLISHED');
      await openSummary(tester, repo: repo);
      await _tap(tester, 'tds-24q');
      await tester.tap(find.byKey(const ValueKey('q24-continue')));
      await tester.pumpAndSettle();
      await _password(tester, 'pw');
      expect(find.textContaining('No month of this quarter has a published payroll run'), findsOneWidget);
    });
  });

  group('preview screen', () {
    Future<Fake5c> openPreview(WidgetTester tester, {Fake5c? repo, FakeDelivery? delivery}) async {
      final r = repo ?? Fake5c();
      await pumpPayroll(
        tester,
        const TdsPreviewScreen(userId: 'u1', fy: '2026-27', name: 'Asha Rao'),
        repo: r,
        perms: _view,
        delivery: delivery,
      );
      return r;
    }

    testWidgets('shows the figures and compares the regimes', (tester) async {
      final repo = await openPreview(tester);
      expect(repo.previewCalls.single, {'user': 'u1', 'fy': '2026-27', 'period': null, 'regime': null});
      expect(find.byKey(const ValueKey('tds-preview-card')), findsOneWidget);
      expect(find.byKey(const ValueKey('regime-comparison')), findsOneWidget);
      // new 30,000 against old 12,500: the old regime is cheaper by 17,500
      expect(find.text('Old regime comes to ₹17,500.00 less for the year.'), findsOneWidget);
      expect(find.byType(SecureScreen), findsWidgets);
    });

    testWidgets('choosing a regime and a month asks again; a month outside the year is refused', (tester) async {
      final repo = await openPreview(tester);
      await tester.tap(find.text('Old'));
      await tester.pumpAndSettle();
      expect(repo.previewCalls.last['regime'], 'old'); // a regime choice recalculates at once
      expect(repo.previewCalls, hasLength(2));
      await _enter(tester, 'tds-period', '2027-04');
      await _tap(tester, 'tds-calculate');
      expect(find.text('The month must be inside FY 2026-27'), findsOneWidget);
      expect(repo.previewCalls, hasLength(2));
      await _enter(tester, 'tds-period', '2026-11');
      await _tap(tester, 'tds-calculate');
      expect(repo.previewCalls.last, {'user': 'u1', 'fy': '2026-27', 'period': '2026-11', 'regime': 'old'});
    });

    testWidgets('the statement asks for the password and is handed over as a file', (tester) async {
      final delivery = FakeDelivery();
      final repo = await openPreview(tester, delivery: delivery);
      await _tap(tester, 'tds-statement');
      await _password(tester, 'pw');
      expect(repo.statementCalls.single, {'user': 'u1', 'fy': '2026-27', 'pw': 'pw'});
      expect(delivery.files.single.filename, 'tax-computation-EMP001-2026-27.pdf');
    });

    testWidgets('the statement with the engine off explains it', (tester) async {
      final repo = Fake5c()..engineError = apiErr('TDS_ENGINE_OFF');
      await openPreview(tester, repo: repo);
      expect(find.textContaining('switched off'), findsOneWidget);
      await _tap(tester, 'tds-statement');
      await _password(tester, 'pw');
      expect(find.textContaining('The TDS engine is switched off'), findsWidgets);
    });
  });

  group('settings', () {
    Map<String, Object?> settings({bool enabled = false, String mode = 'suggest'}) => {
          'payroll.rounding': 'rupee',
          tdsEngineEnabledKey: enabled,
          tdsEngineModeKey: mode,
          tdsOneTimeKey: true,
          tdsProofsFromMonthKey: 1,
          tdsIncludeOtherIncomeKey: false,
        };

    testWidgets('the TDS engine group is shown with its explanation', (tester) async {
      final repo = Fake5c()..settingsMap = settings();
      await pumpPayroll(tester, const Scaffold(body: PayrollSettingsTab()), repo: repo, perms: _manage);
      await tester.scrollUntilVisible(find.byKey(const ValueKey('tds-settings-help')), 300, scrollable: find.byType(Scrollable).first);
      expect(find.textContaining('needs the administrator role and your password'), findsOneWidget);
      expect(find.text('Count only verified proofs from month (1 = January)'), findsOneWidget);
    });

    testWidgets('switching the engine on needs role.assign and the password', (tester) async {
      final repo = Fake5c()..settingsMap = settings();
      await pumpPayroll(
        tester,
        const Scaffold(body: PayrollSettingsTab()),
        repo: repo,
        perms: const [..._manage, 'role.assign'],
      );
      final sw = find.widgetWithText(SwitchListTile, 'Work out TDS with the TDS engine');
      await tester.scrollUntilVisible(sw, 300, scrollable: find.byType(Scrollable).first);
      await tester.tap(sw);
      await tester.pump();
      await tester.ensureVisible(find.byKey(const ValueKey('save-settings')));
      await tester.tap(find.byKey(const ValueKey('save-settings')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 600));
      await _password(tester, 'pw');
      expect(repo.savedSettings.single, {tdsEngineEnabledKey: true});
      expect(repo.settingsPasswords.single, 'pw');
    });

    testWidgets('without role.assign the engine switch is disabled', (tester) async {
      final repo = Fake5c()..settingsMap = settings();
      await pumpPayroll(tester, const Scaffold(body: PayrollSettingsTab()), repo: repo, perms: _manage);
      final sw = find.widgetWithText(SwitchListTile, 'Work out TDS with the TDS engine');
      await tester.scrollUntilVisible(sw, 300, scrollable: find.byType(Scrollable).first);
      expect(tester.widget<SwitchListTile>(sw).onChanged, isNull);
    });

    testWidgets('mode, month and the two switches need role.assign and the password', (tester) async {
      final repo = Fake5c()..settingsMap = settings(enabled: true);
      await pumpPayroll(
        tester,
        const Scaffold(body: PayrollSettingsTab()),
        repo: repo,
        perms: const [..._manage, 'role.assign'],
      );
      final month = find.widgetWithText(TextFormField, 'Count only verified proofs from month (1 = January)');
      await tester.scrollUntilVisible(month, 300, scrollable: find.byType(Scrollable).first);
      await tester.enterText(month, '13');
      await _tap(tester, 'save-settings');
      expect(find.text('At most 12'), findsOneWidget);
      await tester.enterText(month, '4');
      final other = find.widgetWithText(SwitchListTile, 'Include the other income employees declare');
      await tester.ensureVisible(other);
      await tester.pumpAndSettle();
      await tester.tap(other);
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.byKey(const ValueKey('save-settings')));
      await tester.tap(find.byKey(const ValueKey('save-settings')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 600));
      await _password(tester, 'pw');
      expect(repo.savedSettings.single, {tdsProofsFromMonthKey: 4, tdsIncludeOtherIncomeKey: true});
      expect(repo.settingsPasswords.single, 'pw');
    });

    testWidgets('without role.assign mode, month and the switches are disabled', (tester) async {
      final repo = Fake5c()..settingsMap = settings(enabled: true);
      await pumpPayroll(tester, const Scaffold(body: PayrollSettingsTab()), repo: repo, perms: _manage);
      final month = find.widgetWithText(TextFormField, 'Count only verified proofs from month (1 = January)');
      await tester.scrollUntilVisible(month, 300, scrollable: find.byType(Scrollable).first);
      expect(tester.widget<TextFormField>(month).enabled, isFalse);
      for (final t in [
        'Charge the tax on bonus and arrears in the month they are paid',
        'Include the other income employees declare',
      ]) {
        expect(tester.widget<SwitchListTile>(find.widgetWithText(SwitchListTile, t)).onChanged, isNull, reason: t);
      }
      final dd = find.byType(DropdownButtonFormField<String>);
      await tester.ensureVisible(dd.last);
      expect(tester.widget<DropdownButton<String>>(find.descendant(of: dd.last, matching: find.byType(DropdownButton<String>))).onChanged, isNull);
    });
  });

  group('TDS challan', () {
    StatutoryMonth month({Map<String, dynamic>? tds}) => StatutoryMonth.fromJson({
          'period': '2026-10',
          'run_status': 'published',
          'items': [
            tds ?? {'type': 'tds', 'amount': '5000.00', 'employees': 2, 'status': 'unpaid'},
          ],
          'blockers': <Object?>[],
        });

    testWidgets('marking TDS paid asks for BSR code, serial and deposit date, together', (tester) async {
      final repo = Fake5c()..duesList = [month()];
      await pumpPayroll(tester, const Scaffold(body: StatutoryTab()), repo: repo, perms: _manage);
      await _tap(tester, 'mark-paid-tds');
      await _enter(tester, 'paid-reference', 'CH/1');
      await tester.tap(find.byKey(const ValueKey('paid-continue')));
      await tester.pumpAndSettle();
      expect(find.text('Enter the BSR code'), findsOneWidget);
      expect(find.text('Enter the challan serial number'), findsOneWidget);
      await _enter(tester, 'paid-bsr', '123456');
      await _enter(tester, 'paid-serial', '12345');
      await _enter(tester, 'paid-deposit', '2026-10-26');
      await tester.tap(find.byKey(const ValueKey('paid-continue')));
      await tester.pumpAndSettle();
      expect(find.text('The BSR code has exactly 7 digits'), findsOneWidget);
      expect(find.text('Not in the future'), findsOneWidget);
      expect(repo.challanCalls, isEmpty);
      await _enter(tester, 'paid-bsr', '0510016');
      await _enter(tester, 'paid-deposit', '2026-10-20');
      await tester.tap(find.byKey(const ValueKey('paid-continue')));
      await tester.pumpAndSettle();
      await _password(tester, 'pw');
      expect(repo.challanCalls.single['bsr_code'], '0510016');
      expect(repo.challanCalls.single['challan_serial'], '12345');
      expect(repo.challanCalls.single['deposited_on'], '2026-10-20');
      expect(repo.challanCalls.single['type'], 'tds');
    });

    testWidgets('other challans keep the old form', (tester) async {
      final repo = Fake5c()
        ..duesList = [
          month(tds: {'type': 'esi', 'amount': '5000.00', 'employees': 2, 'status': 'unpaid'}),
        ];
      await pumpPayroll(tester, const Scaffold(body: StatutoryTab()), repo: repo, perms: _manage);
      await _tap(tester, 'mark-paid-esi');
      expect(find.byKey(const ValueKey('paid-bsr')), findsNothing);
      expect(find.byKey(const ValueKey('paid-serial')), findsNothing);
    });

    testWidgets('a paid TDS challan shows the BSR code as the API masks it', (tester) async {
      final repo = Fake5c()
        ..duesList = [
          month(tds: {
            'type': 'tds',
            'amount': '5000.00',
            'employees': 2,
            'status': 'paid',
            'paid_on': '2026-10-07',
            'reference': 'CH1',
            'bsr_code': 'XXX0016',
            'challan_serial': '42',
            'deposited_on': '2026-10-06',
          },),
        ];
      await pumpPayroll(tester, const Scaffold(body: StatutoryTab()), repo: repo, perms: _manage);
      expect(find.text('BSR code XXX0016  -  Serial 42  -  Deposited 2026-10-06'), findsOneWidget);
      expect(find.byKey(const ValueKey('tds-challan-missing')), findsNothing);
    });

    testWidgets('a TDS challan paid before 5c is flagged: the 24Q file needs the details', (tester) async {
      final repo = Fake5c()
        ..duesList = [
          month(tds: {'type': 'tds', 'amount': '5000.00', 'employees': 2, 'status': 'paid', 'paid_on': '2026-10-07', 'reference': 'CH1'}),
        ];
      await pumpPayroll(tester, const Scaffold(body: StatutoryTab()), repo: repo, perms: _manage);
      expect(find.byKey(const ValueKey('tds-challan-missing')), findsOneWidget);
    });

    testWidgets('the server refusing the details is explained', (tester) async {
      final repo = Fake5c()
        ..duesList = [month()]
        ..challanError = apiErr('VALIDATION_ERROR', status: 422, details: const [FieldError('bsr_code', 'bad')]);
      await pumpPayroll(tester, const Scaffold(body: StatutoryTab()), repo: repo, perms: _manage);
      await _tap(tester, 'mark-paid-tds');
      await _enter(tester, 'paid-reference', 'CH/1');
      await _enter(tester, 'paid-bsr', '0510016');
      await _enter(tester, 'paid-serial', '1');
      await _enter(tester, 'paid-deposit', '2026-10-20');
      await tester.tap(find.byKey(const ValueKey('paid-continue')));
      await tester.pumpAndSettle();
      await _password(tester, 'pw');
      expect(find.text('The BSR code has exactly 7 digits.'), findsOneWidget);
    });
  });

  group('employee: tax estimate', () {
    Future<Fake5c> openMyTax(WidgetTester tester, {Fake5c? repo}) async {
      final r = repo ?? Fake5c();
      await pumpPayroll(tester, const MyTaxScreen(), repo: r, perms: const ['payroll.view_own']);
      await _tap(tester, 'tab-estimate');
      return r;
    }

    testWidgets('is labelled an estimate, not Form 16, and compares the regimes', (tester) async {
      await openMyTax(tester);
      expect(find.byKey(const ValueKey('estimate-note')), findsOneWidget);
      expect(find.textContaining('It is not Form 16'), findsOneWidget);
      expect(find.byKey(const ValueKey('estimate-card')), findsOneWidget);
      expect(find.text('Estimated tax for the year'), findsOneWidget);
      expect(find.text('₹30,000.00'), findsWidgets);
      expect(find.byKey(const ValueKey('regime-comparison')), findsOneWidget);
      expect(find.text('Old regime comes to ₹17,500.00 less for the year.'), findsOneWidget);
    });

    testWidgets('shows only the flags an employee can act on, and never an override', (tester) async {
      final repo = Fake5c()..comp = tdsJson(flags: ['NO_DECLARATION', 'TDS_OVERRIDDEN', 'RULES_CHANGED']);
      await openMyTax(tester, repo: repo);
      expect(find.byKey(const ValueKey('estimate-flag-NO_DECLARATION')), findsOneWidget);
      expect(find.byKey(const ValueKey('estimate-flag-TDS_OVERRIDDEN')), findsNothing);
      expect(find.byKey(const ValueKey('estimate-flag-RULES_CHANGED')), findsNothing);
      expect(find.byKey(const ValueKey('tds-override-note')), findsNothing);
    });

    testWidgets('no regime comparison when the API sends none', (tester) async {
      final repo = Fake5c()..other = {};
      await openMyTax(tester, repo: repo);
      expect(find.byKey(const ValueKey('regime-comparison')), findsNothing);
      expect(find.byKey(const ValueKey('estimate-card')), findsOneWidget);
    });

    testWidgets('engine off is a calm message', (tester) async {
      final repo = Fake5c()..projectionError = apiErr('TDS_ENGINE_OFF');
      await openMyTax(tester, repo: repo);
      expect(find.text('Tax estimates are not switched on in your company yet.'), findsOneWidget);
      expect(find.text('Try again'), findsNothing);
    });

    testWidgets('a failure has a retry', (tester) async {
      final repo = Fake5c()..projectionError = apiErr('SERVER_ERROR', status: 500);
      await openMyTax(tester, repo: repo);
      expect(find.text('Try again'), findsOneWidget);
    });

    testWidgets('the screen is screenshot-protected', (tester) async {
      await openMyTax(tester);
      expect(find.byType(SecureScreen), findsWidgets);
    });
  });
}
