import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/admin/domain/admin_models.dart';
import 'package:employee_connect/features/admin/presentation/master_screen.dart';
import 'package:employee_connect/features/leave/domain/leave_models.dart';
import 'package:employee_connect/features/leave/presentation/balance_tab.dart';
import 'package:employee_connect/features/payroll/data/import_file_picker.dart';
import 'package:employee_connect/features/payroll/domain/pay_items_models.dart';
import 'package:employee_connect/features/payroll/domain/payroll_models.dart';
import 'package:employee_connect/features/payroll/presentation/arrears_section.dart';
import 'package:employee_connect/features/payroll/presentation/encashment_section.dart';
import 'package:employee_connect/features/payroll/presentation/loans_section.dart';
import 'package:employee_connect/features/payroll/presentation/my_loans_screen.dart';
import 'package:employee_connect/features/payroll/presentation/one_time_section.dart';
import 'package:employee_connect/features/payroll/presentation/pay_items_tab.dart';
import 'package:employee_connect/features/payroll/presentation/payroll_home_screen.dart';
import 'package:employee_connect/features/payroll/presentation/payroll_providers.dart' show payrollSettingsProvider;
import 'package:employee_connect/features/payroll/presentation/payslip_edit_screen.dart';
import 'package:employee_connect/features/payroll/presentation/payslips_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/payroll_fakes.dart';

/// Widget tests for work package 3a (Pay items tab, My loans, payslip automatic
/// lines, leave type fields). Data comes from the HAND-WRITTEN fixtures in
/// `payroll_phase3_contract_samples.json` (from api-contract.md section 17).
const _own = ['payroll.view_own'];
const _view = ['payroll.view_all'];
const _manage = ['payroll.view_all', 'payroll.manage'];

late Map<String, dynamic> samples;

List<Map<String, dynamic>> _items(String key) =>
    ((samples[key] as Map<String, dynamic>)['items'] as List).cast<Map<String, dynamic>>();

Future<void> _tapKey(WidgetTester t, String key) async {
  final f = find.byKey(ValueKey(key));
  if (f.evaluate().isEmpty) {
    await t.scrollUntilVisible(f, 300, scrollable: find.byType(Scrollable).first);
  }
  await t.ensureVisible(f);
  await t.tap(f);
  await t.pumpAndSettle();
}

Future<void> _enter(WidgetTester t, String key, String text) async {
  await t.enterText(find.byKey(ValueKey(key)), text);
  await t.pump();
}

Future<void> _password(WidgetTester t, String pw) async {
  expect(find.byKey(const ValueKey('reauth-password')), findsOneWidget);
  await t.enterText(find.byKey(const ValueKey('reauth-password')), pw);
  await t.tap(find.text('Confirm'));
  await t.pumpAndSettle();
}

PayrollEmployee _employee({String id = 'u1', String eid = 'EMP001', String name = 'Asha Rao'}) =>
    PayrollEmployee.fromJson({
      'user': {'id': id, 'employee_id': eid, 'full_name': name},
      'status': 'active',
      'include_in_payroll': true,
    });

Future<void> _pickEmployee(WidgetTester t, String fieldKey, {String id = 'u1'}) async {
  await t.tap(find.byKey(ValueKey(fieldKey)));
  await t.pumpAndSettle();
  await t.tap(find.byKey(ValueKey('pick-$id')));
  await t.pumpAndSettle();
}

FakeRepo _repo() => FakeRepo()..employeeList = [_employee(), _employee(id: 'me', eid: 'ADM001', name: 'Me')];

void main() {
  setUpAll(() {
    samples = jsonDecode(File('test/fixtures/payroll_phase3_contract_samples.json').readAsStringSync())
        as Map<String, dynamic>;
  });

  group('Pay items tab', () {
    testWidgets('the payroll home screen has a Pay items tab with its sections (overtime only when on)',
        (tester) async {
      await pumpPayroll(tester, const PayrollHomeScreen(), repo: _repo(), perms: _view);
      await tester.tap(find.text('Pay items'));
      await tester.pumpAndSettle();
      for (final s in PaySection.values.where((s) => s != PaySection.overtime)) {
        final chip = find.byKey(ValueKey('pay-section-${s.name}'));
        if (chip.evaluate().isEmpty) {
          // the chip row scrolls sideways; later chips are built on demand
          await tester.scrollUntilVisible(
            chip,
            200,
            scrollable: find.descendant(of: find.byType(PayItemsTab), matching: find.byType(Scrollable)).first,
          );
        }
        expect(chip, findsOneWidget);
      }
      expect(find.byKey(const ValueKey('pay-section-overtime')), findsNothing);
      expect(find.text('One-time'), findsOneWidget);
      expect(find.text('Encashment'), findsOneWidget);
    });

    testWidgets('sections switch', (tester) async {
      final repo = _repo()..loanList = _items('loan_list').map(Loan.fromJson).toList();
      await pumpPayroll(tester, const Scaffold(body: PayItemsTab()), repo: repo, perms: _view);
      await _tapKey(tester, 'pay-section-loans');
      expect(find.textContaining('L-0007'), findsOneWidget);
    });
  });

  group('one-time items', () {
    FakeRepo withItems() => _repo()..itemList = _items('items_page').map(OneTimeItem.fromJson).toList();

    testWidgets('lists items with status, amount, run hint and flags; a viewer has no actions', (tester) async {
      await pumpPayroll(tester, const Scaffold(body: OneTimeSection(canManage: false)), repo: withItems(), perms: _view);
      expect(find.text('Diwali bonus'), findsOneWidget);
      expect(find.textContaining('₹5,000.00'), findsOneWidget);
      expect(find.text('- ₹800.00'), findsOneWidget); // a deduction
      expect(find.text('Lands after the run is reopened'), findsOneWidget);
      expect(find.textContaining('will not be paid until moved'), findsOneWidget);
      expect(find.text('From a bulk import'), findsOneWidget);
      expect(find.text('Paid'), findsOneWidget);
      expect(find.byKey(const ValueKey('items-add')), findsNothing);
      expect(find.byKey(const ValueKey('items-import')), findsNothing);
      expect(find.byKey(const ValueKey('item-menu-i1111111-1111-1111-1111-111111111111')), findsNothing);
      expect(find.byKey(const ValueKey('items-batches')), findsOneWidget);
    });

    testWidgets('empty and error states', (tester) async {
      await pumpPayroll(tester, const Scaffold(body: OneTimeSection(canManage: true)), repo: _repo(), perms: _manage);
      expect(find.text('No pay items for October 2026.'), findsOneWidget);
    });

    testWidgets('a failed load offers a retry', (tester) async {
      final bad = _repo()..itemsError = const ApiException(code: 'SERVICE_UNAVAILABLE', message: '', status: 503);
      await pumpPayroll(tester, const Scaffold(body: OneTimeSection(canManage: true)), repo: bad, perms: _manage);
      expect(find.text('Try again'), findsOneWidget);
    });

    testWidgets('the month bar re-queries; filters narrow the query', (tester) async {
      final repo = withItems();
      await pumpPayroll(tester, const Scaffold(body: OneTimeSection(canManage: true)), repo: repo, perms: _manage);
      expect(repo.itemQueries.last.period, '2026-10');
      await _tapKey(tester, 'items-next');
      expect(repo.itemQueries.last.period, '2026-11');
      await _tapKey(tester, 'items-prev');
      await _tapKey(tester, 'items-prev');
      expect(repo.itemQueries.last.period, '2026-09');
      await tester.tap(find.byKey(const ValueKey('items-code')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Bonus').last);
      await tester.pumpAndSettle();
      expect(repo.itemQueries.last.code, 'BONUS');
    });

    testWidgets('add validates, then creates with a normalised amount', (tester) async {
      final repo = withItems();
      await pumpPayroll(tester, const Scaffold(body: OneTimeSection(canManage: true)), repo: repo, perms: _manage);
      await _tapKey(tester, 'items-add');
      await _tapKey(tester, 'item-save');
      expect(find.text('Choose an employee'), findsOneWidget);
      expect(find.text('Label is required'), findsOneWidget);
      expect(repo.itemCalls, isEmpty);
      await _pickEmployee(tester, 'item-employee');
      // the signed-in user is never offered
      await _enter(tester, 'item-label', 'Diwali bonus');
      await _enter(tester, 'item-amount', '5000');
      await _tapKey(tester, 'item-save');
      expect(repo.itemCalls.single, {
        'op': 'create',
        'user': 'u1',
        'period': '2026-10',
        'code': 'BONUS',
        'label': 'Diwali bonus',
        'amount': '5000.00',
      });
      expect(find.textContaining('Item added for Asha Rao'), findsOneWidget);
    });

    testWidgets('the employee picker leaves the signed-in user out', (tester) async {
      await pumpPayroll(tester, const Scaffold(body: OneTimeSection(canManage: true)), repo: _repo(), perms: _manage);
      await _tapKey(tester, 'items-add');
      await tester.tap(find.byKey(const ValueKey('item-employee')));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('pick-u1')), findsOneWidget);
      expect(find.byKey(const ValueKey('pick-me')), findsNothing);
    });

    testWidgets('an amount of zero is refused locally', (tester) async {
      final repo = withItems();
      await pumpPayroll(tester, const Scaffold(body: OneTimeSection(canManage: true)), repo: repo, perms: _manage);
      await _tapKey(tester, 'items-add');
      await _pickEmployee(tester, 'item-employee');
      await _enter(tester, 'item-label', 'Gift');
      await _enter(tester, 'item-amount', '0');
      await _tapKey(tester, 'item-save');
      expect(repo.itemCalls, isEmpty);
    });

    testWidgets('edit sends the expected version and only the changed field', (tester) async {
      final repo = withItems();
      await pumpPayroll(tester, const Scaffold(body: OneTimeSection(canManage: true)), repo: repo, perms: _manage);
      await _tapKey(tester, 'item-menu-i1111111-1111-1111-1111-111111111111');
      await tester.tap(find.text('Edit'));
      await tester.pumpAndSettle();
      await _enter(tester, 'item-amount', '6000.50');
      await _tapKey(tester, 'item-save');
      expect(repo.itemCalls.single, {
        'op': 'update',
        'id': 'i1111111-1111-1111-1111-111111111111',
        'v': 1,
        'period': null,
        'label': null,
        'amount': '6000.50',
      });
    });

    testWidgets('only pending items can be edited or cancelled', (tester) async {
      await pumpPayroll(tester, const Scaffold(body: OneTimeSection(canManage: true)), repo: withItems(), perms: _manage);
      expect(find.byKey(const ValueKey('item-menu-i1111111-1111-1111-1111-111111111111')), findsOneWidget);
      expect(find.byKey(const ValueKey('item-menu-i3333333-3333-3333-3333-333333333333')), findsNothing);
    });

    testWidgets('cancel asks for a reason and shows ITEM_LOCKED in plain words', (tester) async {
      final repo = withItems()..writeError = const ApiException(code: 'ITEM_LOCKED', message: 'raw', status: 409);
      await pumpPayroll(tester, const Scaffold(body: OneTimeSection(canManage: true)), repo: repo, perms: _manage);
      await _tapKey(tester, 'item-menu-i1111111-1111-1111-1111-111111111111');
      await tester.tap(find.text('Cancel item'));
      await tester.pumpAndSettle();
      await _enter(tester, 'reason-field', 'x');
      await tester.tap(find.widgetWithText(FilledButton, 'Cancel item'));
      await tester.pumpAndSettle();
      expect(find.textContaining('Give a reason'), findsOneWidget);
      await _enter(tester, 'reason-field', 'Duplicate entry');
      await tester.tap(find.widgetWithText(FilledButton, 'Cancel item'));
      await tester.pumpAndSettle();
      expect(find.textContaining('already in a payslip'), findsOneWidget);
      expect(find.text('raw'), findsNothing);
    });
  });

  group('bulk import', () {
    final bytes = Uint8List.fromList([1, 2, 3, 4]);
    PickedWorkbook picked() => PickedWorkbook(name: 'bonus.xlsx', bytes: bytes);

    Future<FakeRepo> openWizard(
      WidgetTester tester, {
      required Map<String, dynamic> report,
      bool matchHash = true,
      FakeDelivery? delivery,
    }) async {
      final file = picked();
      final json = {...report, if (matchHash) 'file_sha256': file.sha256Hex};
      final repo = _repo()..dryReport = ImportReport.fromJson(json);
      await pumpPayroll(
        tester,
        const Scaffold(body: OneTimeSection(canManage: true)),
        repo: repo,
        perms: _manage,
        picker: FakePicker(file),
        delivery: delivery,
      );
      await _tapKey(tester, 'items-import');
      return repo;
    }

    testWidgets('template download goes to the delivery', (tester) async {
      final delivery = FakeDelivery();
      final repo = await openWizard(tester, report: samples['import_dry_run_clean'] as Map<String, dynamic>, delivery: delivery);
      await _tapKey(tester, 'import-template');
      expect(repo.calls, contains('template'));
      expect(delivery.files.single.filename, 'payroll-items-template.xlsx');
    });

    testWidgets('check is disabled until a file is chosen; the report lists row errors and blocks the import',
        (tester) async {
      final repo = await openWizard(tester, report: samples['import_dry_run'] as Map<String, dynamic>);
      expect(tester.widget<FilledButton>(find.byKey(const ValueKey('import-check'))).onPressed, isNull);
      await _tapKey(tester, 'import-pick');
      expect(find.text('bonus.xlsx'), findsOneWidget);
      await _tapKey(tester, 'import-check');
      expect(repo.importCalls.single['op'], 'dry');
      expect(repo.importCalls.single['len'], 4);
      expect(find.text('120 rows, 118 valid, 2 with errors'), findsOneWidget);
      expect(find.text('Row 14: Employee ID not found'), findsOneWidget);
      expect(find.text('Row 40: Amount must be above 0 with at most 2 decimals'), findsOneWidget);
      expect(find.textContaining('₹5,50,000.00'), findsOneWidget);
      expect(tester.widget<FilledButton>(find.byKey(const ValueKey('import-commit'))).onPressed, isNull);
      expect(repo.importCalls.where((c) => c['op'] == 'commit'), isEmpty);
    });

    testWidgets('a clean report commits with the password and the dry run hash', (tester) async {
      final repo = await openWizard(tester, report: samples['import_dry_run_clean'] as Map<String, dynamic>);
      await _tapKey(tester, 'import-pick');
      await _tapKey(tester, 'import-check');
      expect(find.text('2 rows, 2 valid, 0 with errors'), findsOneWidget);
      expect(find.text('Lands after the run is reopened.'), findsOneWidget);
      await _tapKey(tester, 'import-commit');
      await _password(tester, 'right');
      final commit = repo.importCalls.last;
      expect(commit['op'], 'commit');
      expect(commit['pw'], 'right');
      expect(commit['sha'], picked().sha256Hex);
      expect(commit['period'], '2026-11'); // the period of the check, not today's
      expect(find.byKey(const ValueKey('import-done')), findsOneWidget);
      expect(find.textContaining('Imported 2 pay items'), findsOneWidget);
      // once done the import cannot be repeated by mistake
      expect(tester.widget<FilledButton>(find.byKey(const ValueKey('import-commit'))).onPressed, isNull);
    });

    testWidgets('a wrong password asks again; the password is not shown anywhere', (tester) async {
      final repo = await openWizard(tester, report: samples['import_dry_run_clean'] as Map<String, dynamic>);
      repo.pwRejects = 1;
      await _tapKey(tester, 'import-pick');
      await _tapKey(tester, 'import-check');
      await _tapKey(tester, 'import-commit');
      await _password(tester, 'wrong');
      expect(find.text('That password is not correct.'), findsOneWidget);
      await _password(tester, 'right');
      expect(repo.importCalls.where((c) => c['op'] == 'commit').map((c) => c['pw']), ['wrong', 'right']);
      expect(find.text('wrong'), findsNothing);
      expect(find.text('right'), findsNothing);
    });

    testWidgets('a dry run hash that does not match the file stops before any password', (tester) async {
      final repo = await openWizard(
        tester,
        report: samples['import_dry_run_clean'] as Map<String, dynamic>,
        matchHash: false,
      );
      await _tapKey(tester, 'import-pick');
      await _tapKey(tester, 'import-check');
      await _tapKey(tester, 'import-commit');
      expect(find.byKey(const ValueKey('reauth-password')), findsNothing);
      expect(find.textContaining('not the one that was checked'), findsOneWidget);
      expect(repo.importCalls.where((c) => c['op'] == 'commit'), isEmpty);
    });

    testWidgets('a server refusal (IMPORT_DUPLICATE, IMPORT_FILE_CHANGED) is shown in words', (tester) async {
      final repo = await openWizard(tester, report: samples['import_dry_run_clean'] as Map<String, dynamic>);
      repo.writeError = const ApiException(code: 'IMPORT_DUPLICATE', message: 'raw', status: 409);
      await _tapKey(tester, 'import-pick');
      await _tapKey(tester, 'import-check');
      await _tapKey(tester, 'import-commit');
      await _password(tester, 'right');
      expect(find.textContaining('already imported'), findsOneWidget);
      expect(find.byKey(const ValueKey('import-done')), findsNothing);
    });

    testWidgets('a rejected file shows the template hint', (tester) async {
      final repo = await openWizard(tester, report: samples['import_dry_run_clean'] as Map<String, dynamic>);
      repo.importError = const ApiException(
        code: 'IMPORT_INVALID',
        message: 'raw',
        status: 422,
        details: [FieldError('file', 'header')],
      );
      await _tapKey(tester, 'import-pick');
      await _tapKey(tester, 'import-check');
      expect(find.textContaining('does not match the template'), findsOneWidget);
      expect(find.byKey(const ValueKey('import-commit')), findsNothing);
    });

    testWidgets('a file over 1 MB is refused before it is sent', (tester) async {
      final repo = _repo();
      await pumpPayroll(
        tester,
        const Scaffold(body: OneTimeSection(canManage: true)),
        repo: repo,
        perms: _manage,
        picker: FakePicker(PickedWorkbook(name: 'big.xlsx', bytes: Uint8List(payrollImportMaxBytes + 1))),
      );
      await _tapKey(tester, 'items-import');
      await _tapKey(tester, 'import-pick');
      expect(find.textContaining('too large'), findsOneWidget);
      expect(tester.widget<FilledButton>(find.byKey(const ValueKey('import-check'))).onPressed, isNull);
    });

    testWidgets('cancelling the file picker changes nothing', (tester) async {
      await pumpPayroll(
        tester,
        const Scaffold(body: OneTimeSection(canManage: true)),
        repo: _repo(),
        perms: _manage,
        picker: FakePicker(null),
      );
      await _tapKey(tester, 'items-import');
      await _tapKey(tester, 'import-pick');
      expect(find.byKey(const ValueKey('import-filename')), findsNothing);
    });

    testWidgets('batches list with counts; cancel needs a reason and reports the split', (tester) async {
      final repo = _repo()..batchList = ImportBatch.parseList(samples['batches'] as Map<String, dynamic>);
      await pumpPayroll(tester, const Scaffold(body: OneTimeSection(canManage: true)), repo: repo, perms: _manage);
      await _tapKey(tester, 'items-batches');
      expect(find.text('Cancelled'), findsOneWidget);
      expect(find.textContaining('Pending 110'), findsOneWidget);
      // only the batch with pending rows can be cancelled
      expect(find.byKey(const ValueKey('batch-cancel-b1')), findsOneWidget);
      expect(find.byKey(const ValueKey('batch-cancel-b0')), findsNothing);
      await _tapKey(tester, 'batch-cancel-b1');
      await _enter(tester, 'reason-field', 'Wrong file');
      await tester.tap(find.widgetWithText(FilledButton, 'Cancel import'));
      await tester.pumpAndSettle();
      expect(repo.batchCancels, ['b1:Wrong file']);
      expect(find.textContaining('1 could not be cancelled'), findsOneWidget);
    });

    testWidgets('a viewer sees batches but cannot cancel', (tester) async {
      final repo = _repo()..batchList = ImportBatch.parseList(samples['batches'] as Map<String, dynamic>);
      await pumpPayroll(tester, const Scaffold(body: OneTimeSection(canManage: false)), repo: repo, perms: _view);
      await _tapKey(tester, 'items-batches');
      expect(find.byKey(const ValueKey('batch-b1')), findsOneWidget);
      expect(find.byKey(const ValueKey('batch-cancel-b1')), findsNothing);
    });
  });

  group('loans', () {
    FakeRepo withLoan() => _repo()
      ..loanList = _items('loan_list').map(Loan.fromJson).toList()
      ..loanDetail = Loan.fromJson(samples['loan_detail'] as Map<String, dynamic>);

    testWidgets('list shows outstanding, next instalment and status', (tester) async {
      await pumpPayroll(tester, const Scaffold(body: LoansSection(canManage: true)), repo: withLoan(), perms: _manage);
      expect(find.text('Loan L-0007  -  Asha Rao'), findsOneWidget);
      expect(find.textContaining('Outstanding ₹25,000.00 of ₹30,000.00'), findsOneWidget);
      expect(find.textContaining('Next December 2026: ₹2,500.00'), findsOneWidget);
      expect(find.widgetWithText(Chip, 'Active'), findsOneWidget);
    });

    testWidgets('empty list', (tester) async {
      await pumpPayroll(tester, const Scaffold(body: LoansSection(canManage: true)), repo: _repo(), perms: _manage);
      expect(find.text('No loans or advances match.'), findsOneWidget);
    });

    testWidgets('create shows a live preview and refuses an EMI above the principal', (tester) async {
      final repo = withLoan();
      await pumpPayroll(tester, const Scaffold(body: LoansSection(canManage: true)), repo: repo, perms: _manage);
      await _tapKey(tester, 'loans-add');
      await _pickEmployee(tester, 'loan-employee');
      await _enter(tester, 'loan-principal', '30000');
      await _enter(tester, 'loan-emi', '40000');
      expect(find.byKey(const ValueKey('loan-emi-error')), findsOneWidget);
      expect(find.byKey(const ValueKey('loan-preview')), findsNothing);
      await _enter(tester, 'loan-emi', '2500');
      expect(find.byKey(const ValueKey('loan-emi-error')), findsNothing);
      expect(find.textContaining('12 instalments, November 2026 to October 2027'), findsOneWidget);
      // reason is required
      await _tapKey(tester, 'loan-save');
      expect(repo.loanCalls, isEmpty);
      expect(find.textContaining('Reason must be 3 to 200'), findsOneWidget);
      await _enter(tester, 'loan-reason', 'Medical emergency');
      await _tapKey(tester, 'loan-save');
      await _password(tester, 'right');
      expect(repo.loanCalls.single, {
        'op': 'create',
        'user': 'u1',
        'kind': 'loan',
        'principal': '30000.00',
        'emi': '2500.00',
        'start': '2026-11',
        'reason': 'Medical emergency',
        'pw': 'right',
      });
    });

    testWidgets('the tenure limit comes from the payroll settings', (tester) async {
      final repo = withLoan()..settingsMap = {'payroll.loan_max_tenure_months': 6};
      await pumpPayroll(tester, const Scaffold(body: LoansSection(canManage: true)), repo: repo, perms: _manage);
      // the settings provider is read when the form opens, so load it first
      final container = ProviderScope.containerOf(tester.element(find.byType(LoansSection)));
      await container.read(payrollSettingsProvider.future);
      await _tapKey(tester, 'loans-add');
      await _enter(tester, 'loan-principal', '1200');
      await _enter(tester, 'loan-emi', '100');
      expect(find.textContaining('12 instalments; the limit is 6 months'), findsOneWidget);
    });

    testWidgets('detail lists instalments; skip only on pending EMIs; a viewer has no actions', (tester) async {
      await pumpPayroll(tester, const LoanDetailScreen(loanId: 'l1'), repo: withLoan(), perms: _view);
      expect(find.byKey(const ValueKey('inst-n1')), findsOneWidget);
      expect(find.textContaining('₹2,500.00  -  In payslip'), findsOneWidget);
      expect(find.textContaining('Medical emergency'), findsOneWidget);
      expect(find.byKey(const ValueKey('skip-n4')), findsNothing);
      expect(find.byKey(const ValueKey('loan-cancel')), findsNothing);
    });

    testWidgets('skip needs a reason and no password', (tester) async {
      final repo = withLoan();
      await pumpPayroll(tester, const LoanDetailScreen(loanId: 'l1'), repo: repo, perms: _manage);
      expect(find.byKey(const ValueKey('skip-n3')), findsNothing); // processing
      expect(find.byKey(const ValueKey('skip-n1')), findsNothing); // paid
      await _tapKey(tester, 'skip-n4');
      await _enter(tester, 'reason-field', 'Festival month');
      await tester.tap(find.widgetWithText(FilledButton, 'Skip'));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('reauth-password')), findsNothing);
      expect(repo.loanCalls.single, {'op': 'skip', 'inst': 'n4', 'reason': 'Festival month', 'v': 3});
    });

    testWidgets('reschedule, foreclose and cancel each ask for the password', (tester) async {
      final repo = withLoan();
      await pumpPayroll(tester, const LoanDetailScreen(loanId: 'l1'), repo: repo, perms: _manage);

      await _tapKey(tester, 'loan-reschedule');
      await _enter(tester, 'resched-emi', '2000');
      await _enter(tester, 'resched-reason', 'Lower EMI');
      await _tapKey(tester, 'resched-continue');
      await _password(tester, 'pw1');

      await _tapKey(tester, 'loan-foreclose');
      await _enter(tester, 'foreclose-reason', 'Settle in December');
      await _tapKey(tester, 'foreclose-continue');
      await _password(tester, 'pw2');

      await _tapKey(tester, 'loan-cancel');
      await _enter(tester, 'reason-field', 'Entered by mistake');
      await tester.tap(find.widgetWithText(FilledButton, 'Cancel loan'));
      await tester.pumpAndSettle();
      await _password(tester, 'pw3');

      expect(repo.loanCalls.map((c) => c['op']), ['reschedule', 'foreclose', 'cancel']);
      expect(repo.loanCalls[0], {
        'op': 'reschedule',
        'emi': '2000.00',
        'from': '2026-12',
        'reason': 'Lower EMI',
        'v': 3,
        'pw': 'pw1',
      });
      expect(repo.loanCalls[1]['mode'], 'payroll');
      expect(repo.loanCalls[1]['period'], '2026-10');
      expect(repo.loanCalls[1]['pw'], 'pw2');
      expect(repo.loanCalls[2]['pw'], 'pw3');
    });

    testWidgets('foreclosing outside payroll validates the date and reference', (tester) async {
      final repo = withLoan();
      await pumpPayroll(tester, const LoanDetailScreen(loanId: 'l1'), repo: repo, perms: _manage);
      await _tapKey(tester, 'loan-foreclose');
      await tester.tap(find.text('Paid outside'));
      await tester.pumpAndSettle();
      await _enter(tester, 'foreclose-reference', 'bad;ref');
      await _enter(tester, 'foreclose-date', '2026-12-01');
      await _enter(tester, 'foreclose-reason', 'Paid by transfer');
      await _tapKey(tester, 'foreclose-continue');
      expect(find.text('Not in the future'), findsOneWidget);
      expect(find.textContaining('Letters, numbers'), findsOneWidget);
      await _enter(tester, 'foreclose-reference', 'UTR123');
      await _enter(tester, 'foreclose-date', '2026-10-20');
      await _tapKey(tester, 'foreclose-continue');
      await _password(tester, 'pw');
      expect(repo.loanCalls.single['mode'], 'outside');
      expect(repo.loanCalls.single['paid_on'], '2026-10-20');
      expect(repo.loanCalls.single['reference'], 'UTR123');
      expect(repo.loanCalls.single['period'], isNull);
    });

    testWidgets('a conflict is explained and the loan reloads', (tester) async {
      final repo = withLoan()..writeError = const ApiException(code: 'VERSION_CONFLICT', message: 'raw', status: 409);
      await pumpPayroll(tester, const LoanDetailScreen(loanId: 'l1'), repo: repo, perms: _manage);
      await _tapKey(tester, 'loan-cancel');
      await _enter(tester, 'reason-field', 'Entered by mistake');
      await tester.tap(find.widgetWithText(FilledButton, 'Cancel loan'));
      await tester.pumpAndSettle();
      await _password(tester, 'pw');
      expect(find.textContaining('Reload'), findsOneWidget);
    });

    testWidgets('a closed loan has no actions', (tester) async {
      final closed = Loan.fromJson({...samples['loan_detail'] as Map<String, dynamic>, 'status': 'closed'});
      final repo = _repo()..loanDetail = closed;
      await pumpPayroll(tester, const LoanDetailScreen(loanId: 'l1'), repo: repo, perms: _manage);
      expect(find.byKey(const ValueKey('loan-cancel')), findsNothing);
      expect(find.byKey(const ValueKey('skip-n4')), findsNothing);
    });
  });

  group('arrears', () {
    FakeRepo withArrears() => _repo()..arrearsList = _items('arrears_page').map(Arrears.fromJson).toList();

    testWidgets('proposals show range, amount, kind and a month by month breakdown', (tester) async {
      await pumpPayroll(tester, const Scaffold(body: ArrearsSection(canManage: true)), repo: withArrears(), perms: _manage);
      expect(find.text('Arrears for August 2026 - October 2026'), findsOneWidget);
      expect(find.text('+ ₹4,800.00'), findsOneWidget);
      expect(find.text('Salary recovery for September 2026'), findsOneWidget);
      expect(find.text('- ₹1,200.00'), findsOneWidget);
      expect(find.text('To be paid in October 2026'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('arrears-breakdown-ar1')));
      await tester.pumpAndSettle();
      expect(find.textContaining('August 2026  (31 paid days)'), findsOneWidget);
      expect(find.text('Earnings on new structure'), findsWidgets);
      await tester.tap(find.byKey(const ValueKey('arrears-breakdown-ar2')));
      await tester.pumpAndSettle();
      expect(find.text('-₹1,200.00'), findsOneWidget); // a signed difference
    });

    testWidgets('a viewer has no buttons', (tester) async {
      await pumpPayroll(tester, const Scaffold(body: ArrearsSection(canManage: false)), repo: withArrears(), perms: _view);
      expect(find.byKey(const ValueKey('arrears-accept-ar1')), findsNothing);
      expect(find.byKey(const ValueKey('arrears-dismiss-ar1')), findsNothing);
    });

    testWidgets('accept picks the month then asks for the password with the version', (tester) async {
      final repo = withArrears()..pwRejects = 0;
      await pumpPayroll(tester, const Scaffold(body: ArrearsSection(canManage: true)), repo: repo, perms: _manage);
      await _tapKey(tester, 'arrears-accept-ar1');
      await _tapKey(tester, 'arrears-accept-continue');
      await _password(tester, 'right');
      expect(repo.arrearsCalls.single, {'op': 'accept', 'id': 'ar1', 'period': '2026-10', 'v': 2, 'pw': 'right'});
      expect(find.textContaining('added to October 2026'), findsOneWidget);
    });

    testWidgets('ARREARS_STALE is explained', (tester) async {
      final repo = withArrears()..writeError = const ApiException(code: 'ARREARS_STALE', message: 'raw', status: 409);
      await pumpPayroll(tester, const Scaffold(body: ArrearsSection(canManage: true)), repo: repo, perms: _manage);
      await _tapKey(tester, 'arrears-accept-ar1');
      await _tapKey(tester, 'arrears-accept-continue');
      await _password(tester, 'right');
      expect(find.textContaining('recalculated'), findsOneWidget);
    });

    testWidgets('dismiss needs a reason; an accepted row can be dismissed but not accepted', (tester) async {
      final repo = withArrears();
      await pumpPayroll(tester, const Scaffold(body: ArrearsSection(canManage: true)), repo: repo, perms: _manage);
      expect(find.byKey(const ValueKey('arrears-accept-ar3')), findsNothing);
      expect(find.byKey(const ValueKey('arrears-dismiss-ar3')), findsOneWidget);
      await _tapKey(tester, 'arrears-dismiss-ar1');
      await _enter(tester, 'reason-field', 'Not applicable');
      await tester.tap(find.widgetWithText(FilledButton, 'Dismiss'));
      await tester.pumpAndSettle();
      expect(repo.arrearsCalls.single, {'op': 'dismiss', 'id': 'ar1', 'reason': 'Not applicable'});
    });

    testWidgets('recalculate says so when nothing differs', (tester) async {
      final repo = withArrears();
      await pumpPayroll(tester, const Scaffold(body: ArrearsSection(canManage: true)), repo: repo, perms: _manage);
      await _tapKey(tester, 'arrears-recalc-ar1');
      expect(repo.arrearsCalls.single, {'op': 'recalc', 'user': 'u1'});
      expect(find.textContaining('No difference found'), findsOneWidget);
    });

    testWidgets('empty state explains where proposals come from', (tester) async {
      await pumpPayroll(tester, const Scaffold(body: ArrearsSection(canManage: true)), repo: _repo(), perms: _manage);
      expect(find.textContaining('after a salary structure change'), findsOneWidget);
    });
  });

  group('leave encashment', () {
    FakeRepo withQuote() => _repo()
      ..encashmentList = _items('encashments_page').map(Encashment.fromJson).toList()
      ..quote = EncashmentQuote.fromJson(samples['encashment_quote'] as Map<String, dynamic>);

    Future<void> chooseType(WidgetTester t) async {
      await t.tap(find.byKey(const ValueKey('enc-type')));
      await t.pumpAndSettle();
      await t.tap(find.text('Earned Leave').last);
      await t.pumpAndSettle();
    }

    testWidgets('list shows days, amount, rate and a cancel button for pending rows', (tester) async {
      await pumpPayroll(tester, const Scaffold(body: EncashmentSection(canManage: true)), repo: withQuote(), perms: _manage);
      expect(find.text('Earned Leave: 4 days for 2026'), findsOneWidget);
      expect(find.textContaining('₹2,666.68'), findsOneWidget);
      expect(find.textContaining('₹666.67 a day'), findsOneWidget);
      expect(find.byKey(const ValueKey('enc-cancel-e1')), findsOneWidget);
    });

    testWidgets('a viewer sees the list only', (tester) async {
      await pumpPayroll(tester, const Scaffold(body: EncashmentSection(canManage: false)), repo: withQuote(), perms: _view);
      expect(find.byKey(const ValueKey('enc-add')), findsNothing);
      expect(find.byKey(const ValueKey('enc-cancel-e1')), findsNothing);
    });

    testWidgets('cancel confirms, then calls the server', (tester) async {
      final repo = withQuote();
      await pumpPayroll(tester, const Scaffold(body: EncashmentSection(canManage: true)), repo: repo, perms: _manage);
      await _tapKey(tester, 'enc-cancel-e1');
      await tester.tap(find.widgetWithText(FilledButton, 'Cancel encashment'));
      await tester.pumpAndSettle();
      expect(repo.encashCalls.single, {'op': 'cancel', 'id': 'e1'});
    });

    testWidgets('the amount comes from the server quote; create needs the quote and the password', (tester) async {
      final repo = withQuote();
      await pumpPayroll(tester, const Scaffold(body: EncashmentSection(canManage: true)), repo: repo, perms: _manage);
      await _tapKey(tester, 'enc-add');
      await _pickEmployee(tester, 'enc-employee');
      await chooseType(tester);
      await _enter(tester, 'enc-days', '1.3');
      await _tapKey(tester, 'enc-quote');
      expect(find.text('Use steps of 0.5 day'), findsOneWidget);
      await _enter(tester, 'enc-days', '4');
      await _enter(tester, 'enc-reason', 'Year-end encashment');
      await _tapKey(tester, 'enc-continue'); // no quote yet
      expect(find.text('Get the quote first.'), findsOneWidget);
      expect(repo.encashCalls.where((c) => c['op'] == 'create'), isEmpty);
      await _tapKey(tester, 'enc-quote');
      expect(find.byKey(const ValueKey('enc-quote-card')), findsOneWidget);
      expect(find.text('₹2,666.68'), findsWidgets);
      expect(find.text('6 days'), findsOneWidget);
      expect(repo.encashCalls.last, {
        'op': 'quote',
        'user': 'u1',
        'type': 'lt1',
        'year': 2026,
        'period': '2026-10',
        'days': 4.0,
      });
      await _tapKey(tester, 'enc-continue');
      await _password(tester, 'right');
      expect(repo.encashCalls.last, {
        'op': 'create',
        'user': 'u1',
        'type': 'lt1',
        'year': 2026,
        'days': 4.0,
        'period': '2026-10',
        'reason': 'Year-end encashment',
        'pw': 'right',
      });
    });

    testWidgets('changing the days drops the old quote', (tester) async {
      await pumpPayroll(tester, const Scaffold(body: EncashmentSection(canManage: true)), repo: withQuote(), perms: _manage);
      await _tapKey(tester, 'enc-add');
      await _pickEmployee(tester, 'enc-employee');
      await chooseType(tester);
      await _tapKey(tester, 'enc-quote'); // days empty: the server proposes all available days
      expect(find.byKey(const ValueKey('enc-quote-card')), findsOneWidget);
      await _enter(tester, 'enc-days', '2');
      expect(find.byKey(const ValueKey('enc-quote-card')), findsNothing);
    });

    testWidgets('a server refusal (INSUFFICIENT_BALANCE) shows the available days', (tester) async {
      final repo = withQuote()
        ..quoteError = const ApiException(
          code: 'INSUFFICIENT_BALANCE',
          message: 'raw',
          status: 409,
          details: [FieldError('available_days', '6.0')],
        );
      await pumpPayroll(tester, const Scaffold(body: EncashmentSection(canManage: true)), repo: repo, perms: _manage);
      await _tapKey(tester, 'enc-add');
      await _pickEmployee(tester, 'enc-employee');
      await chooseType(tester);
      await _enter(tester, 'enc-days', '9');
      await _tapKey(tester, 'enc-quote');
      expect(find.textContaining('6.0 days are available'), findsOneWidget);
    });

    testWidgets('without an encashable leave type the form says what to do', (tester) async {
      final repo = withQuote()..encashableTypes = const [];
      await pumpPayroll(tester, const Scaffold(body: EncashmentSection(canManage: true)), repo: repo, perms: _manage);
      await _tapKey(tester, 'enc-add');
      expect(find.byKey(const ValueKey('enc-no-types')), findsOneWidget);
    });
  });

  group('employee: my loans', () {
    FakeRepo mine() => _repo()
      ..myLoanList = MyLoan.parseList(samples['me_loans'] as Map<String, dynamic>)
      ..myLoanDetail = MyLoan.fromJson(samples['me_loan'] as Map<String, dynamic>);

    testWidgets('lists loans read-only and opens the schedule', (tester) async {
      await pumpPayroll(tester, const MyLoansScreen(), repo: mine(), perms: _own);
      expect(find.text('Loan L-0007'), findsOneWidget);
      expect(find.text('Advance L-0002'), findsOneWidget);
      expect(find.textContaining('Outstanding ₹25,000.00 of ₹30,000.00'), findsOneWidget);
      expect(find.textContaining('Next December 2026: ₹2,500.00'), findsOneWidget);
      expect(find.byType(FloatingActionButton), findsNothing);
      await tester.tap(find.byKey(const ValueKey('my-loan-l1')));
      await tester.pumpAndSettle();
      expect(find.text('Deducted'), findsOneWidget);
      expect(find.text('Deferred'), findsOneWidget);
      expect(find.text('Scheduled'), findsOneWidget);
      expect(find.text('Outstanding'), findsOneWidget);
      // no admin actions and no reason
      expect(find.text('Cancel loan'), findsNothing);
      expect(find.textContaining('Medical'), findsNothing);
    });

    testWidgets('empty and error states', (tester) async {
      await pumpPayroll(tester, const MyLoansScreen(), repo: _repo(), perms: _own);
      expect(find.text('You have no loans or advances.'), findsOneWidget);
      final bad = _repo()..myLoansError = const ApiException(code: 'SERVICE_UNAVAILABLE', message: '', status: 503);
      await pumpPayroll(tester, const MyLoansScreen(), repo: bad, perms: _own);
      expect(find.text('Try again'), findsOneWidget);
    });

    testWidgets('without payroll.view_own nothing is requested', (tester) async {
      await pumpPayroll(tester, const MyLoansScreen(), repo: mine(), perms: const []);
      expect(find.text('You do not have permission to view this.'), findsOneWidget);
    });

    testWidgets('it is reachable from the Payslips screen', (tester) async {
      await pumpPayroll(tester, const PayslipsScreen(), repo: mine(), perms: _own);
      await _tapKey(tester, 'open-my-loans');
      expect(find.text('My loans'), findsWidgets);
      expect(find.text('Loan L-0007'), findsOneWidget);
    });
  });

  group('admin payslip: automatic lines and warnings', () {
    Payslip withComponents({List<String> warnings = const ['ARREARS_PROPOSED', 'MANUAL_COMPONENT_OVERLAP']}) {
      final j = payslipJson(admin: true, status: 'draft');
      j['inputs'] = samples['admin_payslip_inputs'];
      j['warnings'] = warnings;
      return Payslip.fromJson(j);
    }

    testWidgets('components are listed read-only with their source ids', (tester) async {
      final repo = FakeRepo()..adminPayslip = withComponents();
      await pumpPayroll(tester, const PayslipEditScreen(payslipId: 'p1'), repo: repo, perms: _manage);
      expect(find.text('Automatic lines'), findsOneWidget);
      expect(find.text('Loan L-0007 (3/12)'), findsOneWidget);
      expect(find.text('Leave encashment (4.0 d)'), findsOneWidget);
      expect(find.text('Arrears Aug 2026 - Oct 2026'), findsOneWidget);
      expect(find.text('Loan instalment 9f1c2d3e'), findsOneWidget);
      expect(find.text('Leave encashment 55556666'), findsOneWidget);
      expect(find.textContaining('Overtime aaaa1111'), findsOneWidget); // 3b: minutes now lead the caption
      // read-only: no text field or remove button belongs to them
      expect(find.byTooltip('Remove adjustment'), findsNothing);
    });

    testWidgets('new warning codes appear as readable chips', (tester) async {
      final repo = FakeRepo()..adminPayslip = withComponents();
      await pumpPayroll(tester, const PayslipEditScreen(payslipId: 'p1'), repo: repo, perms: _manage);
      expect(find.text('Arrears proposed: review in Pay items'), findsOneWidget);
      expect(find.textContaining('may duplicate an automatic line'), findsOneWidget);
    });

    testWidgets('saving never sends components back', (tester) async {
      final repo = FakeRepo()..adminPayslip = withComponents();
      await pumpPayroll(tester, const PayslipEditScreen(payslipId: 'p1'), repo: repo, perms: _manage);
      await tester.enterText(find.widgetWithText(TextFormField, 'TDS (monthly)'), '1300');
      await _tapKey(tester, 'save-payslip');
      final patch = repo.savedPatches.single;
      expect(patch['tds'], '1300.00');
      expect(jsonEncode(patch), isNot(contains('components')));
      expect(jsonEncode(patch), isNot(contains('9f1c2d3e')));
    });

    testWidgets('a payslip without components has no such card', (tester) async {
      final repo = FakeRepo()..adminPayslip = Payslip.fromJson(payslipJson(admin: true, status: 'draft'));
      await pumpPayroll(tester, const PayslipEditScreen(payslipId: 'p1'), repo: repo, perms: _manage);
      expect(find.text('Automatic lines'), findsNothing);
    });
  });

  group('leave: encashment fields', () {
    testWidgets('the leave type form has the three encashment fields', (tester) async {
      Map<String, Object?>? saved;
      final rec = ((samples['leave_types_page'] as Map<String, dynamic>)['items'] as List).first
          as Map<String, dynamic>;
      tester.view.physicalSize = const Size(800, 3000);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: MasterFormDialog(
              spec: leaveTypeSpec,
              initial: rec,
              onSave: (body) async {
                saved = body;
                return null;
              },
            ),
          ),
        ),
      );
      expect(find.text('Can be encashed (paid out)'), findsOneWidget);
      expect(find.text('Encashment cap per year (days, empty = no cap)'), findsOneWidget);
      expect(find.text('Days that must stay unencashed'), findsOneWidget);
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();
      expect(saved!['encashable'], true);
      expect(saved!['encash_max_days_per_year'], 10.0);
      expect(saved!['encash_min_balance'], 2.0);
    });

    testWidgets('a bad cap is refused', (tester) async {
      var called = false;
      tester.view.physicalSize = const Size(800, 3000);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: MasterFormDialog(
              spec: leaveTypeSpec,
              onSave: (body) async {
                called = true;
                return null;
              },
            ),
          ),
        ),
      );
      final fields = find.byType(TextFormField);
      // code, name, quota, attachment, max consecutive, cap, min balance
      await tester.enterText(fields.at(0), 'EL');
      await tester.enterText(fields.at(1), 'Earned');
      await tester.enterText(fields.at(5), '10.3');
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();
      expect(called, isFalse);
      expect(find.text('Use steps of 0.5'), findsOneWidget);
    });

    testWidgets('the leave balance card shows encashed days', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(
            body: LeaveBalanceCard(
              balance: LeaveBalanceItem(
                leaveTypeId: 't1',
                code: 'EL',
                name: 'Earned Leave',
                quota: 18,
                used: 3,
                pending: 1,
                encashed: 2,
                remaining: 12,
              ),
            ),
          ),
        ),
      );
      expect(find.text('Encashed 2'), findsOneWidget);
      expect(find.text('12 left'), findsOneWidget);
    });

    testWidgets('no encashed text when nothing was encashed', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(
            body: LeaveBalanceCard(
              balance: LeaveBalanceItem(
                leaveTypeId: 't1',
                code: 'CL',
                name: 'Casual',
                quota: 12,
                used: 3,
                pending: 1,
                remaining: 8,
              ),
            ),
          ),
        ),
      );
      expect(find.textContaining('Encashed'), findsNothing);
    });
  });
}
