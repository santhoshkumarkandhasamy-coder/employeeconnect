import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/security/screen_protect.dart';
import 'package:employee_connect/features/admin/domain/admin_models.dart' show EmployeeRef;
import 'package:employee_connect/features/payroll/data/payroll_delivery.dart' show DeliveryOutcome;
import 'package:employee_connect/features/payroll/domain/payroll_models.dart';
import 'package:employee_connect/features/payroll/presentation/employee_payroll_screen.dart';
import 'package:employee_connect/features/payroll/presentation/payroll_home_screen.dart';
import 'package:employee_connect/features/payroll/presentation/payroll_settings_tab.dart';
import 'package:employee_connect/features/payroll/presentation/payslip_detail_screen.dart';
import 'package:employee_connect/features/payroll/presentation/payslip_edit_screen.dart';
import 'package:employee_connect/features/payroll/presentation/payslips_screen.dart';
import 'package:employee_connect/features/payroll/presentation/pt_slab_editor.dart';
import 'package:employee_connect/features/payroll/presentation/run_review_screen.dart';
import 'package:employee_connect/features/payroll/presentation/runs_tab.dart';
import 'package:employee_connect/features/payroll/presentation/structure_form_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/payroll_fakes.dart';

const _own = ['payroll.view_own'];
const _view = ['payroll.view_all'];
const _manage = ['payroll.view_all', 'payroll.manage'];
const _all = ['payroll.view_all', 'payroll.manage', 'payroll.publish'];

Future<void> _tapKey(WidgetTester t, String key) async {
  final f = find.byKey(ValueKey(key));
  if (f.evaluate().isEmpty) {
    await t.scrollUntilVisible(f, 300, scrollable: find.byType(Scrollable).first);
  }
  await t.ensureVisible(f);
  await t.tap(find.byKey(ValueKey(key)));
  await t.pumpAndSettle();
}

void main() {
  group('employee payslips', () {
    testWidgets('lists months without any amount and marks revisions', (tester) async {
      final repo = FakeRepo()
        ..mine = [
          PayslipListItem.fromJson({
            'id': 'a',
            'period': '2026-10',
            'revision': 2,
            'published_at': '2026-10-31T09:00:00Z',
            'revised': true,
            'pay_date': '2026-10-31',
          }),
          PayslipListItem.fromJson({
            'id': 'b',
            'period': '2026-09',
            'revision': 1,
            'published_at': '2026-09-30T09:00:00Z',
            'revised': false,
          }),
        ];
      await pumpPayroll(tester, const PayslipsScreen(), repo: repo, perms: _own);
      expect(find.text('October 2026'), findsOneWidget);
      expect(find.text('September 2026'), findsOneWidget);
      expect(find.text('Revised'), findsOneWidget);
      expect(find.textContaining('₹'), findsNothing);
      expect(find.text('2026'), findsOneWidget); // year selector
      expect(repo.calls, contains('myPayslips:2026'));
    });

    testWidgets('changing the year loads that year', (tester) async {
      final repo = FakeRepo();
      await pumpPayroll(tester, const PayslipsScreen(), repo: repo, perms: _own);
      await tester.tap(find.byTooltip('Previous year'));
      await tester.pumpAndSettle();
      expect(repo.calls, contains('myPayslips:2025'));
      expect(find.byTooltip('Next year'), findsOneWidget);
    });

    testWidgets('empty state', (tester) async {
      await pumpPayroll(tester, const PayslipsScreen(), repo: FakeRepo(), perms: _own);
      expect(find.text('No payslips yet'), findsOneWidget);
    });

    testWidgets('error state offers retry', (tester) async {
      final repo = FakeRepo()
        ..listError = const ApiException(code: 'NETWORK_ERROR', message: 'No connection.');
      await pumpPayroll(tester, const PayslipsScreen(), repo: repo, perms: _own);
      expect(find.text('No connection.'), findsOneWidget);
      repo.listError = null;
      await tester.tap(find.text('Try again'));
      await tester.pumpAndSettle();
      expect(find.text('No payslips yet'), findsOneWidget);
    });

    testWidgets('without payroll.view_own nothing is requested', (tester) async {
      final repo = FakeRepo();
      await pumpPayroll(tester, const PayslipsScreen(), repo: repo, perms: const []);
      expect(find.textContaining('permission'), findsOneWidget);
      expect(repo.calls, isEmpty);
    });

    testWidgets('the screen holds FLAG_SECURE while it is visible', (tester) async {
      await pumpPayroll(tester, const PayslipsScreen(), repo: FakeRepo(), perms: _own);
      expect(ScreenProtect.holders, greaterThan(0));
      await tester.pumpWidget(const SizedBox());
      await tester.pumpAndSettle();
      expect(ScreenProtect.holders, 0);
    });

    testWidgets('detail shows amounts, net pay in words and both buttons fetch the PDF',
        (tester) async {
      final repo = FakeRepo()..myDetail = Payslip.fromJson(payslipJson());
      final delivery = FakeDelivery();
      await pumpPayroll(
        tester,
        const PayslipDetailScreen(id: 'p1'),
        repo: repo,
        perms: _own,
        delivery: delivery,
      );
      expect(find.text('Payslip for October 2026'), findsOneWidget);
      expect(find.text('₹35,300.00'), findsOneWidget);
      expect(find.text('Rupees Thirty-Five Thousand Three Hundred Only'), findsOneWidget);
      expect(find.text('₹19,032.00'), findsOneWidget);
      expect(find.text('XXXXX1234X'), findsOneWidget);

      await _tapKey(tester, 'download-pdf');
      expect(repo.calls.where((c) => c == 'myPdf:p1'), hasLength(1));
      expect(delivery.files.single.filename, 'payslip-EMP001-2026-10.pdf');

      await _tapKey(tester, 'share-pdf');
      expect(delivery.files, hasLength(2));
    });

    testWidgets('a cancelled share tells the user the file was not kept', (tester) async {
      final repo = FakeRepo()..myDetail = Payslip.fromJson(payslipJson());
      final delivery = FakeDelivery()..outcome = DeliveryOutcome.cancelled;
      await pumpPayroll(
        tester,
        const PayslipDetailScreen(id: 'p1'),
        repo: repo,
        perms: _own,
        delivery: delivery,
      );
      await _tapKey(tester, 'share-pdf');
      expect(find.textContaining('not kept'), findsOneWidget);
    });

    testWidgets('a revised payslip shows the reason and links to the earlier one', (tester) async {
      final repo = FakeRepo()
        ..myDetail = Payslip.fromJson(payslipJson(
          revision: 2,
          revisionInfo: {'reason': 'Leave approved late', 'supersedes_id': 'old', 'superseded_by_id': null},
        ),);
      await pumpPayroll(tester, const PayslipDetailScreen(id: 'p1'), repo: repo, perms: _own);
      expect(find.textContaining('Leave approved late'), findsOneWidget);
      expect(find.text('View the earlier revision'), findsOneWidget);
    });

    testWidgets('a superseded payslip points to the latest revision', (tester) async {
      final repo = FakeRepo()
        ..myDetail = Payslip.fromJson(payslipJson(
          status: 'superseded',
          revisionInfo: {'reason': null, 'supersedes_id': null, 'superseded_by_id': 'new'},
        ),);
      await pumpPayroll(tester, const PayslipDetailScreen(id: 'p1'), repo: repo, perms: _own);
      expect(find.textContaining('replaced by a newer revision'), findsOneWidget);
      expect(find.text('View the latest revision'), findsOneWidget);
    });

    testWidgets('detail 404 shows an error without a retry', (tester) async {
      final repo = FakeRepo()
        ..listError = const ApiException(code: 'NOT_FOUND', message: 'x', status: 404);
      await pumpPayroll(tester, const PayslipDetailScreen(id: 'zz'), repo: repo, perms: _own);
      expect(find.textContaining('not available'), findsOneWidget);
      expect(find.text('Try again'), findsNothing);
    });
  });

  group('payroll home and runs', () {
    testWidgets('tabs appear with view_all; New run only with manage', (tester) async {
      final repo = FakeRepo();
      await pumpPayroll(tester, const PayrollHomeScreen(), repo: repo, perms: _view);
      expect(find.text('Runs'), findsOneWidget);
      expect(find.text('Employees'), findsOneWidget);
      expect(find.text('Settings'), findsOneWidget);
      expect(find.text('New run'), findsNothing);
      expect(find.text('No payroll runs for this year.'), findsOneWidget);
    });

    testWidgets('without view_all the area is closed', (tester) async {
      await pumpPayroll(tester, const PayrollHomeScreen(), repo: FakeRepo(), perms: _own);
      expect(find.textContaining('permission'), findsOneWidget);
      expect(find.text('Runs'), findsNothing);
    });

    testWidgets('runs list shows status and net total; manage can create a run', (tester) async {
      final repo = FakeRepo()
        ..runList = [
          PayrollRunItem.fromJson({
            'id': 'r1',
            'period': '2026-09',
            'status': 'published',
            'employees': 48,
            'net_pay_total': '1690000.00',
            'published_at': '2026-09-30T09:00:00Z',
          }),
        ];
      await pumpPayroll(tester, const RunsTab(), repo: repo, perms: _manage);
      expect(find.text('September 2026'), findsOneWidget);
      expect(find.text('Published'), findsOneWidget);
      expect(find.textContaining('₹16,90,000.00'), findsOneWidget);

      await tester.tap(find.text('New run'));
      await tester.pumpAndSettle();
      expect(find.text('New payroll run'), findsOneWidget);
      await tester.tap(find.byTooltip('Previous month'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Create'));
      await tester.pumpAndSettle();
      expect(repo.calls, contains('createRun:2026-09:null'));
      expect(find.text('Payroll run'), findsNothing); // moved on to the review screen
    });
  });

  group('run review', () {
    testWidgets('draft run: totals, flags, Process for managers', (tester) async {
      final repo = FakeRepo()
        ..rows = [
          RunPayslipRow.fromJson(rowJson()),
          RunPayslipRow.fromJson(rowJson(id: 'p2', errors: ['STRUCTURE_MISSING'], net: null)),
        ];
      await pumpPayroll(tester, const RunReviewScreen(runId: 'r1'), repo: repo, perms: _manage);
      expect(find.text('₹16,90,000.00'), findsOneWidget);
      expect(find.text('No salary structure'), findsOneWidget);
      expect(find.byKey(const ValueKey('process-run')), findsOneWidget);
      expect(find.byKey(const ValueKey('publish-run')), findsNothing);
      await _tapKey(tester, 'process-run');
      expect(repo.calls, contains('process'));
    });

    testWidgets('read-only viewer sees no write actions but can export', (tester) async {
      await pumpPayroll(tester, const RunReviewScreen(runId: 'r1'), repo: FakeRepo(), perms: _view);
      expect(find.byKey(const ValueKey('process-run')), findsNothing);
      expect(find.text('Refresh'), findsNothing);
      expect(find.byKey(const ValueKey('register-run')), findsOneWidget);
    });

    testWidgets('PAYROLL_BLOCKED lists the employees', (tester) async {
      final repo = FakeRepo()
        ..processError = const ApiException(
          code: 'PAYROLL_BLOCKED',
          message: 'blocked',
          status: 409,
          details: [FieldError('EMP014', 'STRUCTURE_MISSING'), FieldError('EMP020', 'NEGATIVE_NET_PAY')],
        );
      await pumpPayroll(tester, const RunReviewScreen(runId: 'r1'), repo: repo, perms: _manage);
      await _tapKey(tester, 'process-run');
      expect(find.text('Payroll cannot be processed'), findsOneWidget);
      expect(find.text('EMP014'), findsOneWidget);
      expect(find.text('Net pay is negative'), findsOneWidget);
    });

    testWidgets('publish asks to confirm, then for the password, and retries a wrong one',
        (tester) async {
      final repo = FakeRepo()
        ..run0 = PayrollRun.fromJson(runJson(status: 'processed'))
        ..wrongPasswords = 1;
      await pumpPayroll(tester, const RunReviewScreen(runId: 'r1'), repo: repo, perms: _all);
      await _tapKey(tester, 'publish-run');
      expect(find.textContaining('Publish October 2026?'), findsOneWidget);
      await tester.tap(find.text('Publish').last);
      await tester.pumpAndSettle();
      expect(find.text('Confirm your password'), findsOneWidget);
      await tester.enterText(find.byKey(const ValueKey('reauth-password')), 'wrong');
      await tester.tap(find.text('Confirm'));
      await tester.pumpAndSettle();
      expect(find.text('That password is not correct.'), findsOneWidget);
      await tester.enterText(find.byKey(const ValueKey('reauth-password')), 'right');
      await tester.tap(find.text('Confirm'));
      await tester.pumpAndSettle();
      expect(repo.publishPasswords, ['wrong', 'right']);
      expect(find.text('Payslips published.'), findsOneWidget);
    });

    testWidgets('publish needs payroll.publish', (tester) async {
      final repo = FakeRepo()..run0 = PayrollRun.fromJson(runJson(status: 'processed'));
      await pumpPayroll(tester, const RunReviewScreen(runId: 'r1'), repo: repo, perms: _manage);
      expect(find.byKey(const ValueKey('publish-run')), findsNothing);
      expect(find.text('Reopen'), findsOneWidget);
    });

    testWidgets('register with bank details needs the password', (tester) async {
      final repo = FakeRepo();
      final delivery = FakeDelivery();
      await pumpPayroll(
        tester,
        const RunReviewScreen(runId: 'r1'),
        repo: repo,
        perms: _manage,
        delivery: delivery,
      );
      await _tapKey(tester, 'register-run');
      await tester.tap(find.byKey(const ValueKey('include-bank')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Download'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byKey(const ValueKey('reauth-password')), 'pw');
      await tester.tap(find.text('Confirm'));
      await tester.pumpAndSettle();
      expect(repo.registerCalls.single.includeBank, isTrue);
      expect(repo.registerCalls.single.pw, 'pw');
      expect(delivery.files.single.filename, 'payroll-register-2026-10.xlsx');
    });

    testWidgets('register without bank details sends no password; viewers cannot include bank',
        (tester) async {
      final repo = FakeRepo();
      await pumpPayroll(tester, const RunReviewScreen(runId: 'r1'), repo: repo, perms: _view);
      await _tapKey(tester, 'register-run');
      expect(find.byKey(const ValueKey('include-bank')), findsNothing);
      await tester.tap(find.text('Download'));
      await tester.pumpAndSettle();
      expect(repo.registerCalls.single.includeBank, isFalse);
      expect(repo.registerCalls.single.pw, isNull);
    });
  });

  group('payslip edit', () {
    Future<FakeRepo> open(WidgetTester tester, {String userId = 'u1', List<String> perms = _all}) async {
      final repo = FakeRepo()
        ..adminPayslip = Payslip.fromJson(payslipJson(status: 'draft', admin: true, userId: userId));
      await pumpPayroll(tester, const PayslipEditScreen(payslipId: 'p1'), repo: repo, perms: perms);
      return repo;
    }

    testWidgets('LOP override is validated: half-day steps, bounds, reason required', (tester) async {
      final repo = await open(tester);
      expect(find.textContaining('Unpaid leave 1'), findsOneWidget);

      await tester.enterText(find.byKey(const ValueKey('lop-override')), '1.3');
      await _tapKey(tester, 'save-payslip');
      expect(find.textContaining('half-day steps'), findsOneWidget);

      await tester.enterText(find.byKey(const ValueKey('lop-override')), '40');
      await _tapKey(tester, 'save-payslip');
      expect(find.textContaining('At most 31'), findsOneWidget);

      await tester.enterText(find.byKey(const ValueKey('lop-override')), '1');
      await tester.pumpAndSettle();
      await _tapKey(tester, 'save-payslip');
      expect(find.textContaining('Give a reason'), findsOneWidget);
      expect(repo.savedPatches, isEmpty);

      await tester.enterText(find.byKey(const ValueKey('lop-reason')), 'Confirmed present that day');
      await _tapKey(tester, 'save-payslip');
      expect(repo.savedPatches.single['lop_override'], 1);
      expect(repo.savedPatches.single['lop_override_reason'], 'Confirmed present that day');
      expect(repo.savedPatches.single['expected_version'], 3);
      expect(repo.savedPatches.single.containsKey('tds'), isFalse); // unchanged keys are not sent
    });

    testWidgets('adding an adjustment sends it as strings', (tester) async {
      final repo = await open(tester);
      await _tapKey(tester, 'add-adjustment');
      final fields = find.byType(TextFormField);
      // label then amount of the new row are the last two TextFormFields before the note.
      await tester.enterText(find.widgetWithText(TextFormField, 'Label (shown as an earning)'), 'Diwali');
      await tester.enterText(find.widgetWithText(TextFormField, 'Amount'), '5000');
      expect(fields, findsWidgets);
      await _tapKey(tester, 'save-payslip');
      expect(repo.savedPatches.single['adjustments'], [
        {'code': 'BONUS', 'label': 'Diwali', 'amount': '5000.00'},
      ]);
    });

    testWidgets('an invalid adjustment amount blocks the save', (tester) async {
      final repo = await open(tester);
      await _tapKey(tester, 'add-adjustment');
      await tester.enterText(find.widgetWithText(TextFormField, 'Label (shown as an earning)'), 'x');
      await tester.enterText(find.widgetWithText(TextFormField, 'Amount'), '1.234');
      await _tapKey(tester, 'save-payslip');
      expect(find.textContaining('Enter an amount like'), findsOneWidget);
      expect(repo.savedPatches, isEmpty);
    });

    testWidgets('saving nothing changed says so', (tester) async {
      final repo = await open(tester);
      await _tapKey(tester, 'save-payslip');
      expect(find.text('Nothing changed.'), findsOneWidget);
      expect(repo.savedPatches, isEmpty);
    });

    testWidgets('VERSION_CONFLICT asks to reload', (tester) async {
      final repo = await open(tester);
      repo.saveError =
          const ApiException(code: 'VERSION_CONFLICT', message: 'stale', status: 409);
      await tester.enterText(find.byKey(const ValueKey('lop-override')), '1');
      await tester.pumpAndSettle();
      await tester.enterText(find.byKey(const ValueKey('lop-reason')), 'Confirmed present');
      await _tapKey(tester, 'save-payslip');
      expect(find.text('Payslip changed'), findsOneWidget);
      expect(find.text('Reload'), findsOneWidget);
      await tester.tap(find.text('Reload'));
      await tester.pumpAndSettle();
      expect(find.text('Payslip changed'), findsNothing);
    });

    testWidgets('your own payslip cannot be edited, processed or published', (tester) async {
      await open(tester, userId: 'me');
      expect(find.byKey(const ValueKey('save-payslip')), findsNothing);
      expect(find.byKey(const ValueKey('process-payslip')), findsNothing);
      expect(find.textContaining('your own payslip'), findsOneWidget);
    });

    testWidgets('a viewer sees the payslip read-only', (tester) async {
      await open(tester, perms: _view);
      expect(find.byKey(const ValueKey('save-payslip')), findsNothing);
      expect(find.byKey(const ValueKey('process-payslip')), findsNothing);
    });

    testWidgets('a published payslip can be revised with a reason', (tester) async {
      final repo = FakeRepo()
        ..adminPayslip = Payslip.fromJson(payslipJson(admin: true))
        ..mine = [];
      await pumpPayroll(
        tester,
        const PayslipEditScreen(payslipId: 'p1'),
        repo: repo,
        perms: _all,
      );
      expect(find.byKey(const ValueKey('save-payslip')), findsNothing);
      await _tapKey(tester, 'revise-payslip');
      await tester.enterText(find.byKey(const ValueKey('reason-field')), 'x');
      await tester.tap(find.text('Create revision'));
      await tester.pumpAndSettle();
      expect(find.textContaining('Give a reason'), findsOneWidget);
      await tester.enterText(find.byKey(const ValueKey('reason-field')), 'Leave approved after payroll');
      await tester.tap(find.text('Create revision'));
      await tester.pumpAndSettle();
      expect(repo.revisedReason, 'Leave approved after payroll');
    });
  });

  group('employees, structure and details', () {
    const emp = EmployeeRef(id: 'u1', employeeId: 'EMP001', fullName: 'Asha Rao');

    testWidgets('masked details by default; Reveal shows the real values', (tester) async {
      final repo = FakeRepo();
      await pumpPayroll(
        tester,
        const EmployeePayrollScreen(employee: emp),
        repo: repo,
        perms: _manage,
      );
      expect(find.text('XXXXX1234X'), findsOneWidget);
      expect(find.text('No salary structure yet. Payroll cannot be processed without one.'), findsOneWidget);
      await _tapKey(tester, 'reveal-details');
      expect(repo.revealCalled, isTrue);
      expect(find.text('ABCDE1234F'), findsOneWidget);
      expect(find.text('XXXXX1234X'), findsNothing);
      await tester.tap(find.text('Hide'));
      await tester.pumpAndSettle();
      expect(find.text('ABCDE1234F'), findsNothing);
    });

    testWidgets('viewers cannot add structures or edit details; self cannot either', (tester) async {
      await pumpPayroll(
        tester,
        const EmployeePayrollScreen(employee: emp),
        repo: FakeRepo(),
        perms: _view,
      );
      expect(find.byKey(const ValueKey('add-structure')), findsNothing);
      expect(find.byKey(const ValueKey('edit-details')), findsNothing);
    });

    testWidgets('managers cannot edit their own payroll data', (tester) async {
      await pumpPayroll(
        tester,
        const EmployeePayrollScreen(employee: emp),
        repo: FakeRepo(),
        perms: _manage,
        selfId: 'u1',
      );
      expect(find.byKey(const ValueKey('add-structure')), findsNothing);
      expect(find.byKey(const ValueKey('edit-details')), findsNothing);
      expect(find.textContaining('cannot change your own'), findsOneWidget);
    });

    testWidgets('structure form: live gross, code rules, and the request body', (tester) async {
      final repo = FakeRepo();
      await pumpPayroll(
        tester,
        const StructureFormScreen(userId: 'u1'),
        repo: repo,
        perms: _manage,
      );
      await tester.enterText(find.widgetWithText(TextFormField, 'Basic (monthly)'), '20000');
      await tester.enterText(find.widgetWithText(TextFormField, 'HRA (monthly)'), '8000');
      await tester.pumpAndSettle();
      expect(find.text('₹28,000.00'), findsOneWidget);

      await tester.tap(find.text('Add').first);
      await tester.pumpAndSettle();
      await tester.enterText(find.widgetWithText(TextFormField, 'Code'), 'basic');
      await tester.enterText(find.widgetWithText(TextFormField, 'Name'), 'Special');
      await tester.enterText(find.widgetWithText(TextFormField, 'Amount (monthly)').last, '10000');
      await tester.pumpAndSettle();
      expect(find.text('₹38,000.00'), findsOneWidget);
      await _tapKey(tester, 'save-structure');
      expect(find.text('BASIC is reserved'), findsOneWidget);
      expect(repo.createdStructures, isEmpty);

      await tester.enterText(find.widgetWithText(TextFormField, 'Code'), 'special');
      await _tapKey(tester, 'save-structure');
      final body = repo.createdStructures.single;
      expect(body['basic'], '20000.00');
      expect(body['hra'], '8000.00');
      expect(body['effective_from'], '2026-10-01');
      expect(body['tds_monthly'], '0.00');
      expect((body['allowances']! as List).single, {
        'code': 'SPECIAL',
        'name': 'Special',
        'amount': '10000.00',
        'prorate': true,
        'esi_wage': true,
      });
    });

    testWidgets('structure form requires a basic above zero', (tester) async {
      final repo = FakeRepo();
      await pumpPayroll(tester, const StructureFormScreen(userId: 'u1'), repo: repo, perms: _manage);
      await tester.enterText(find.widgetWithText(TextFormField, 'Basic (monthly)'), '0');
      await _tapKey(tester, 'save-structure');
      expect(find.textContaining('must be more than 0'), findsOneWidget);
      expect(repo.createdStructures, isEmpty);
    });

    testWidgets('an in-use structure is read-only', (tester) async {
      final s = SalaryStructure.fromJson({
        'id': 's1',
        'user_id': 'u1',
        'effective_from': '2026-04-01',
        'basic': '20000.00',
        'hra': '8000.00',
        'monthly_gross': '28000.00',
        'in_use': true,
      });
      await pumpPayroll(
        tester,
        StructureFormScreen(userId: 'u1', existing: s),
        repo: FakeRepo(),
        perms: _manage,
      );
      expect(find.textContaining('cannot be'), findsOneWidget);
      expect(find.byKey(const ValueKey('save-structure')), findsNothing);
      expect(find.text('Copy as a new structure'), findsOneWidget);
    });
  });

  group('settings', () {
    Map<String, Object?> settings() => {
          'payroll.pay_days_basis': 'calendar',
          'payroll.lop_absent_days': true,
          'payroll.pf_employee_rate_pct': 12,
          'payroll.pf_wage_ceiling': 15000,
          'payroll.pt_frequency': 'half_yearly',
          'payroll.pt_slabs': [
            {'min_gross': '0.00', 'max_gross': '21000.00', 'amount': '0.00', 'months': [3, 9]},
            {'min_gross': '21001.00', 'max_gross': null, 'amount': '135.00', 'months': [3, 9]},
          ],
          'payroll.rounding': 'rupee',
          'payroll.company_address': '',
        };

    testWidgets('only changed settings are saved; slabs stay as strings', (tester) async {
      final repo = FakeRepo()..settingsMap = settings();
      await pumpPayroll(tester, const Scaffold(body: PayrollSettingsTab()), repo: repo, perms: _manage);
      await tester.enterText(find.widgetWithText(TextFormField, 'PF employee rate'), '10.5');
      await _tapKey(tester, 'save-settings');
      expect(repo.savedSettings.single, {'payroll.pf_employee_rate_pct': 10.5});
    });

    testWidgets('maker-checker switch: needs role.assign and asks for the password (13.3)',
        (tester) async {
      final repo = FakeRepo()
        ..settingsMap = {...settings(), 'payroll.publish_requires_second_person': false};
      // payroll.manage alone: the switch is locked
      await pumpPayroll(tester, const Scaffold(body: PayrollSettingsTab()), repo: repo, perms: _manage);
      final tile = find.widgetWithText(SwitchListTile, 'Publisher must differ from the person who processed');
      await tester.scrollUntilVisible(tile, 300, scrollable: find.byType(Scrollable).first);
      expect(tester.widget<SwitchListTile>(tile).onChanged, isNull);
      expect(find.text('Only an administrator can change this.'), findsOneWidget);
    });

    testWidgets('an administrator flips maker-checker with their password', (tester) async {
      final repo = FakeRepo()
        ..settingsMap = {...settings(), 'payroll.publish_requires_second_person': false};
      await pumpPayroll(
        tester,
        const Scaffold(body: PayrollSettingsTab()),
        repo: repo,
        perms: [..._manage, 'role.assign'],
      );
      final tile = find.widgetWithText(SwitchListTile, 'Publisher must differ from the person who processed');
      await tester.scrollUntilVisible(tile, 300, scrollable: find.byType(Scrollable).first);
      await tester.tap(tile);
      await tester.pumpAndSettle();
      await tester.drag(find.byType(ListView).first, const Offset(0, -3000));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('save-settings')));
      await tester.pump(const Duration(milliseconds: 600));
      await tester.pump(const Duration(seconds: 1)); // dialog transition
      expect(find.text('Confirm your password'), findsOneWidget);
      await tester.enterText(find.byKey(const ValueKey('reauth-password')), 'secret');
      await tester.tap(find.text('Confirm'));
      await tester.pumpAndSettle();
      expect(repo.savedSettings.single, {'payroll.publish_requires_second_person': true});
      expect(repo.settingsPasswords.single, 'secret');
    });

    testWidgets('other settings are saved without a password prompt', (tester) async {
      final repo = FakeRepo()..settingsMap = settings();
      await pumpPayroll(tester, const Scaffold(body: PayrollSettingsTab()), repo: repo, perms: _manage);
      await tester.enterText(find.widgetWithText(TextFormField, 'PF employee rate'), '11');
      await _tapKey(tester, 'save-settings');
      expect(find.text('Confirm your password'), findsNothing);
      expect(repo.settingsPasswords.single, isNull);
    });

    testWidgets('out-of-range numbers are rejected before the request', (tester) async {
      final repo = FakeRepo()..settingsMap = settings();
      await pumpPayroll(tester, const Scaffold(body: PayrollSettingsTab()), repo: repo, perms: _manage);
      await tester.enterText(find.widgetWithText(TextFormField, 'PF employee rate'), '25');
      await _tapKey(tester, 'save-settings');
      expect(find.textContaining('At most 20'), findsOneWidget);
      expect(repo.savedSettings, isEmpty);
    });

    testWidgets('slab editor: add a slab, toggle months, then save the table', (tester) async {
      final repo = FakeRepo()..settingsMap = settings();
      await pumpPayroll(tester, const Scaffold(body: PayrollSettingsTab()), repo: repo, perms: _manage);
      expect(find.text('Slab 2'), findsOneWidget);
      await _tapKey(tester, 'add-slab');
      expect(find.text('Slab 3'), findsOneWidget);
      // incomplete new slab is rejected
      await _tapKey(tester, 'save-settings');
      expect(find.textContaining('Slab 3: enter a valid minimum'), findsOneWidget);
      expect(repo.savedSettings, isEmpty);
    });

    testWidgets('slab months chips toggle and are saved', (tester) async {
      final repo = FakeRepo()..settingsMap = settings();
      await pumpPayroll(tester, const Scaffold(body: PayrollSettingsTab()), repo: repo, perms: _manage);
      await tester.ensureVisible(find.byKey(const ValueKey('slab-0-month-6')));
      await tester.tap(find.byKey(const ValueKey('slab-0-month-6')));
      await tester.pumpAndSettle();
      await _tapKey(tester, 'save-settings');
      final slabs = repo.savedSettings.single['payroll.pt_slabs']! as List;
      expect((slabs.first as Map)['months'], [3, 6, 9]);
      expect((slabs.first as Map)['amount'], '0.00');
      expect((slabs.last as Map)['max_gross'], isNull);
    });

    testWidgets('Tamil Nadu defaults button fills the table', (tester) async {
      final repo = FakeRepo()..settingsMap = {...settings(), 'payroll.pt_slabs': <Object?>[]};
      await pumpPayroll(tester, const Scaffold(body: PayrollSettingsTab()), repo: repo, perms: _manage);
      expect(find.textContaining('No slabs'), findsOneWidget);
      await _tapKey(tester, 'tn-defaults');
      expect(find.text('Slab 6'), findsOneWidget);
      await _tapKey(tester, 'save-settings');
      expect((repo.savedSettings.single['payroll.pt_slabs']! as List), hasLength(6));
    });

    testWidgets('viewers see the settings without a save button', (tester) async {
      final repo = FakeRepo()..settingsMap = settings();
      await pumpPayroll(tester, const Scaffold(body: PayrollSettingsTab()), repo: repo, perms: _view);
      expect(find.byKey(const ValueKey('save-settings')), findsNothing);
      expect(find.textContaining('view these settings'), findsOneWidget);
    });
  });

  testWidgets('PtSlabEditor reports every edit', (tester) async {
    List<PtSlab>? last;
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: SingleChildScrollView(
            child: PtSlabEditor(initial: const [], onChanged: (v) => last = v),
          ),
        ),
      ),
    );
    await tester.tap(find.byKey(const ValueKey('add-slab')));
    await tester.pumpAndSettle();
    expect(last, hasLength(1));
    await tester.enterText(find.widgetWithText(TextField, 'From gross'), '100');
    await tester.enterText(find.widgetWithText(TextField, 'Tax amount'), '50');
    expect(last!.single.minGross, '100.00');
    expect(last!.single.amount, '50.00');
    expect(last!.single.maxGross, isNull);
    await tester.tap(find.text('Every month'));
    await tester.pumpAndSettle();
    expect(last!.single.months, isEmpty);
  });
}
