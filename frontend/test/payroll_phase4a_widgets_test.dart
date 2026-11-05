import 'dart:typed_data';

import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/admin/domain/admin_models.dart' show EmployeeRef, NamedRef;
import 'package:employee_connect/features/admin/presentation/admin_providers.dart' show departmentRefsProvider;
import 'package:employee_connect/features/attendance/domain/attendance_models.dart' show Paged;
import 'package:employee_connect/features/payroll/data/import_file_picker.dart';
import 'package:employee_connect/features/payroll/data/payroll_repository.dart';
import 'package:employee_connect/features/payroll/domain/payroll_models.dart';
import 'package:employee_connect/features/payroll/domain/phase4a_models.dart';
import 'package:employee_connect/features/payroll/presentation/approvals_tab.dart';
import 'package:employee_connect/features/payroll/presentation/batch_screens.dart';
import 'package:employee_connect/features/payroll/presentation/bulk_hike_wizard_screen.dart';
import 'package:employee_connect/features/payroll/presentation/ctc_calculator_screen.dart';
import 'package:employee_connect/features/payroll/presentation/employee_payroll_screen.dart';
import 'package:employee_connect/features/payroll/presentation/gratuity_screen.dart';
import 'package:employee_connect/features/payroll/presentation/increment_detail_screen.dart';
import 'package:employee_connect/features/payroll/presentation/increment_form_screen.dart';
import 'package:employee_connect/features/payroll/presentation/payroll_home_screen.dart';
import 'package:employee_connect/features/payroll/presentation/structure_form_screen.dart';
import 'package:employee_connect/features/payroll/presentation/structure_import_wizard_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/payroll_fakes.dart';

const _emp = {'id': 'u1', 'employee_id': 'EMP001', 'full_name': 'Asha Rao'};
const _maker = {'id': 'maker', 'employee_id': 'EMP009', 'full_name': 'Maker'};

Map<String, dynamic> _inc({
  String id = 'i1',
  String status = 'draft',
  Map<String, dynamic>? createdBy = _maker,
  Map<String, dynamic>? submittedBy,
  String? batchId,
  int version = 2,
}) =>
    {
      'id': id,
      'user': _emp,
      'batch_id': batchId,
      'kind': 'percent',
      'effective_from': '2027-04-01',
      'percent': '10.00',
      'method': 'proportional',
      'proposed': {'basic': '22000.00', 'hra': '8800.00', 'allowances': <Object?>[], 'monthly_gross': '44000.00'},
      'change': {'old_gross': '40000.00', 'new_gross': '44000.00', 'amount': '4000.00', 'percent': '10.00'},
      'diff': [
        {'code': 'BASIC', 'old': '20000.00', 'new': '22000.00'},
      ],
      'warnings': ['BACKDATED'],
      'reason': 'Annual increment',
      'status': status,
      'run_status': 'none',
      'created_by': createdBy,
      'submitted_by': submittedBy,
      'version': version,
    };

Map<String, dynamic> _batch({int submitted = 2, String id = 'b1'}) => {
      'id': id,
      'kind': 'percent',
      'effective_from': '2027-04-01',
      'percent': '8.00',
      'method': 'proportional',
      'rows': 2,
      'reason': 'FY',
      'counts': {'draft': 0, 'submitted': submitted},
      'totals': {
        'old_gross': '80000.00',
        'new_gross': '86400.00',
        'delta_monthly': '6400.00',
        'delta_annual': '76800.00',
      },
      'created_by': _maker,
    };

class Fake4a extends FakeRepo {
  Increment current = Increment.fromJson(_inc());
  ApiException? approveError;
  ApiException? createStructureError;
  final approvals = <Map<String, Object?>>[];
  final rejects = <Map<String, Object?>>[];
  final calls4a = <String>[];
  List<Increment> list = [];
  List<IncrementBatch> batches = [];
  BatchApproveResult batchApproveResult = const BatchApproveResult(approved: 2, failed: [], arrearsProposed: 0);
  BulkHikeInput? hikeInput;
  ApiException? hikeError;
  BulkHikeResult hikeResult = BulkHikeResult.fromJson({
    'batch': _batch(),
    'created': 1,
    'skipped': [
      {'user': _emp, 'reason': 'NO_STRUCTURE'},
    ],
  });
  CtcResult? ctc;
  ApiException? ctcError;
  GratuityPage? gratuityPage;
  List<SalaryHistoryEntry> history = [];
  StructureImportReport? importReport;
  final importCommits = <Map<String, Object?>>[];

  @override
  Future<Paged<Increment>> increments(IncrementQuery q, {int page = 1}) async =>
      Paged(items: list, page: 1, total: list.length, pageSize: 20);

  @override
  Future<Increment> increment(String id) async => current;

  @override
  Future<Increment> submitIncrement(String id, {required int expectedVersion}) async {
    calls4a.add('submit:$expectedVersion');
    return current;
  }

  @override
  Future<Increment> approveIncrement(
    String id, {
    required int expectedVersion,
    required String currentPassword,
    String? comment,
  }) async {
    approvals.add({'v': expectedVersion, 'pw': currentPassword, 'comment': comment});
    if (pwRejects > 0) {
      pwRejects--;
      throw const ApiException(code: 'REAUTH_FAILED', message: 'no', status: 403);
    }
    if (approveError != null) throw approveError!;
    return Increment.fromJson({..._inc(status: 'approved'), 'arrears_proposed': true});
  }

  @override
  Future<Increment> rejectIncrement(String id, {required int expectedVersion, required String comment}) async {
    rejects.add({'v': expectedVersion, 'comment': comment});
    return Increment.fromJson(_inc(status: 'rejected'));
  }

  @override
  Future<Paged<IncrementBatch>> incrementBatches({String? kind, int page = 1}) async =>
      Paged(items: batches, page: 1, total: batches.length, pageSize: 20);

  @override
  Future<IncrementBatch> incrementBatch(String id) async => IncrementBatch.fromJson(_batch());

  @override
  Future<BatchApproveResult> approveIncrementBatch(String id, {required String currentPassword, String? comment}) async {
    approvals.add({'batch': id, 'pw': currentPassword});
    return batchApproveResult;
  }

  @override
  Future<BulkHikeResult> bulkHike(BulkHikeInput input) async {
    hikeInput = input;
    if (hikeError != null) throw hikeError!;
    return hikeResult;
  }

  @override
  Future<CtcResult> ctcCalculate(CtcInput input) async {
    if (ctcError != null) throw ctcError!;
    return ctc!;
  }

  @override
  Future<GratuityPage> gratuity(GratuityQuery q, {int page = 1}) async => gratuityPage!;

  @override
  Future<List<SalaryHistoryEntry>> salaryHistory(String userId) async => history;

  @override
  Future<SalaryStructure> createStructure(String userId, StructureInput input) async {
    if (createStructureError != null) throw createStructureError!;
    return super.createStructure(userId, input);
  }

  @override
  Future<PayrollFile> structuresTemplate() async {
    calls.add('structuresTemplate');
    return PayrollFile(Uint8List.fromList([1]), 'salary-structures-template.xlsx');
  }

  @override
  Future<StructureImportReport> structureImportDryRun({required List<int> bytes, required String filename}) async =>
      importReport!;

  @override
  Future<StructureImportCommit> structureImportCommit({
    required List<int> bytes,
    required String filename,
    required String fileSha256,
    required String reason,
    required String currentPassword,
  }) async {
    importCommits.add({'sha': fileSha256, 'reason': reason, 'pw': currentPassword});
    return const StructureImportCommit(batchId: 'b', created: 3, incrementsSubmitted: 2, arrearsProposed: 1);
  }
}

const _manage = ['payroll.view_all', 'payroll.manage'];
const _approve = ['payroll.view_all', 'payroll.approve'];

Future<void> _password(WidgetTester tester, String pw) async {
  await tester.enterText(find.byKey(const ValueKey('reauth-password')), pw);
  await tester.tap(find.text('Confirm'));
  await tester.pumpAndSettle();
}

Future<void> _comment(WidgetTester tester, String text) async {
  if (text.isNotEmpty) await tester.enterText(find.byKey(const ValueKey('comment-field')), text);
  await tester.tap(find.byKey(const ValueKey('comment-confirm')));
  await tester.pumpAndSettle();
}

void main() {
  group('increment detail', () {
    testWidgets('draft: diff table, warnings, maker actions, no approve', (tester) async {
      final repo = Fake4a();
      await pumpPayroll(tester, const IncrementDetailScreen(incrementId: 'i1'), repo: repo, perms: _manage);
      expect(find.byKey(const ValueKey('increment-diff')), findsOneWidget);
      expect(find.text('Backdated: arrears will be proposed'), findsOneWidget);
      expect(find.text('₹22,000.00'), findsOneWidget);
      expect(find.byKey(const ValueKey('inc-submit')), findsOneWidget);
      expect(find.byKey(const ValueKey('inc-approve')), findsNothing);
      await tester.tap(find.byKey(const ValueKey('inc-submit')));
      await tester.pumpAndSettle();
      expect(repo.calls4a, ['submit:2']);
    });

    testWidgets('without payroll.approve there are no approve buttons', (tester) async {
      final repo = Fake4a()..current = Increment.fromJson(_inc(status: 'submitted'));
      await pumpPayroll(tester, const IncrementDetailScreen(incrementId: 'i1'), repo: repo, perms: _manage);
      expect(find.byKey(const ValueKey('inc-approve')), findsNothing);
      expect(find.byKey(const ValueKey('inc-withdraw')), findsOneWidget);
    });

    testWidgets('approve asks for a comment then the password and sends the version', (tester) async {
      final repo = Fake4a()..current = Increment.fromJson(_inc(status: 'submitted'));
      await pumpPayroll(tester, const IncrementDetailScreen(incrementId: 'i1'), repo: repo, perms: _approve);
      await tester.tap(find.byKey(const ValueKey('inc-approve')));
      await tester.pumpAndSettle();
      await _comment(tester, 'looks fine');
      await _password(tester, 'secret');
      expect(repo.approvals.single, {'v': 2, 'pw': 'secret', 'comment': 'looks fine'});
      expect(find.textContaining('Arrears were proposed'), findsOneWidget);
    });

    testWidgets('a wrong password asks again', (tester) async {
      final repo = Fake4a()
        ..current = Increment.fromJson(_inc(status: 'submitted'))
        ..pwRejects = 1;
      await pumpPayroll(tester, const IncrementDetailScreen(incrementId: 'i1'), repo: repo, perms: _approve);
      await tester.tap(find.byKey(const ValueKey('inc-approve')));
      await tester.pumpAndSettle();
      await _comment(tester, '');
      await _password(tester, 'bad');
      expect(find.text('That password is not correct.'), findsOneWidget);
      await _password(tester, 'good');
      expect(repo.approvals.length, 2);
    });

    testWidgets('the creator or submitter cannot approve; the server error is still mapped', (tester) async {
      final repo = Fake4a()..current = Increment.fromJson(_inc(status: 'submitted'));
      await pumpPayroll(
        tester,
        const IncrementDetailScreen(incrementId: 'i1'),
        repo: repo,
        perms: _approve,
        selfId: 'maker',
      );
      expect(find.byKey(const ValueKey('inc-approve')), findsNothing);
      expect(find.byKey(const ValueKey('cannot-decide')), findsOneWidget);
    });

    testWidgets('VERSION_CONFLICT and SECOND_PERSON_REQUIRED show clear messages', (tester) async {
      final repo = Fake4a()
        ..current = Increment.fromJson(_inc(status: 'submitted'))
        ..approveError = const ApiException(code: 'VERSION_CONFLICT', message: 'x', status: 409);
      await pumpPayroll(tester, const IncrementDetailScreen(incrementId: 'i1'), repo: repo, perms: _approve);
      await tester.tap(find.byKey(const ValueKey('inc-approve')));
      await tester.pumpAndSettle();
      await _comment(tester, '');
      await _password(tester, 'pw');
      expect(find.textContaining('Someone else changed this record'), findsOneWidget);

      repo.approveError = const ApiException(code: 'SECOND_PERSON_REQUIRED', message: 'x', status: 403);
      await tester.tap(find.byKey(const ValueKey('inc-approve')));
      await tester.pumpAndSettle();
      await _comment(tester, '');
      await _password(tester, 'pw');
      expect(find.textContaining('different person'), findsOneWidget);
    });

    testWidgets('reject needs a comment', (tester) async {
      final repo = Fake4a()..current = Increment.fromJson(_inc(status: 'submitted'));
      await pumpPayroll(tester, const IncrementDetailScreen(incrementId: 'i1'), repo: repo, perms: _approve);
      await tester.tap(find.byKey(const ValueKey('inc-reject')));
      await tester.pumpAndSettle();
      await _comment(tester, '');
      expect(find.textContaining('A comment is required'), findsOneWidget);
      expect(repo.rejects, isEmpty);
      await _comment(tester, 'Too high');
      expect(repo.rejects.single, {'v': 2, 'comment': 'Too high'});
    });
  });

  group('approvals', () {
    testWidgets('queue lists increments (not batch rows) and batches', (tester) async {
      final repo = Fake4a()
        ..list = [Increment.fromJson(_inc()), Increment.fromJson(_inc(id: 'i2', batchId: 'b1'))]
        ..batches = [IncrementBatch.fromJson(_batch()), IncrementBatch.fromJson(_batch(submitted: 0, id: 'b2'))];
      await pumpPayroll(tester, const Scaffold(body: ApprovalsTab()), repo: repo, perms: _approve);
      expect(find.byKey(const ValueKey('increment-i1')), findsOneWidget);
      expect(find.byKey(const ValueKey('increment-i2')), findsNothing);
      expect(find.byKey(const ValueKey('batch-b1')), findsOneWidget);
      expect(find.byKey(const ValueKey('batch-b2')), findsNothing);
      expect(find.byKey(const ValueKey('queue-approve-i1')), findsOneWidget);
    });

    testWidgets('empty queue', (tester) async {
      await pumpPayroll(tester, const Scaffold(body: ApprovalsTab()), repo: Fake4a(), perms: _approve);
      expect(find.text('Nothing is waiting for your approval.'), findsOneWidget);
    });

    testWidgets('the Approvals tab shows only with payroll.approve', (tester) async {
      await pumpPayroll(tester, const PayrollHomeScreen(), repo: Fake4a(), perms: _manage);
      expect(find.byKey(const ValueKey('tab-approvals')), findsNothing);
      await pumpPayroll(tester, const PayrollHomeScreen(), repo: Fake4a(), perms: _approve);
      expect(find.byKey(const ValueKey('tab-approvals')), findsOneWidget);
    });

    testWidgets('batch approve shows failed rows', (tester) async {
      final repo = Fake4a()
        ..batchApproveResult = const BatchApproveResult(
          approved: 1,
          failed: [IdReason(id: 'abcdef123456', reason: 'INCREMENT_STALE')],
          arrearsProposed: 0,
        );
      await pumpPayroll(tester, const BatchDetailScreen(batchId: 'b1'), repo: repo, perms: _approve);
      await tester.tap(find.byKey(const ValueKey('batch-approve')));
      await tester.pumpAndSettle();
      await _comment(tester, '');
      await _password(tester, 'pw');
      expect(find.byKey(const ValueKey('batch-approve-result')), findsOneWidget);
      expect(find.text('1 approved'), findsOneWidget);
      expect(find.textContaining('structure changed'), findsOneWidget);
    });

    testWidgets('the batch maker cannot approve', (tester) async {
      await pumpPayroll(tester, const BatchDetailScreen(batchId: 'b1'), repo: Fake4a(), perms: _approve, selfId: 'maker');
      expect(find.byKey(const ValueKey('batch-approve')), findsNothing);
      expect(find.byKey(const ValueKey('batch-maker-note')), findsOneWidget);
    });
  });

  group('forms', () {
    testWidgets('increment form validates the percent and shows an estimate', (tester) async {
      final repo = Fake4a()
        ..structureList = [
          SalaryStructure.fromJson({'id': 's1', 'effective_from': '2026-04-01', 'monthly_gross': '40000.00'}),
        ];
      await pumpPayroll(tester, const IncrementFormScreen(userId: 'u1', userName: 'Asha'), repo: repo, perms: _manage);
      await tester.tap(find.byKey(const ValueKey('inc-save')));
      await tester.pumpAndSettle();
      expect(find.text('Percent is required'), findsOneWidget);
      expect(find.text('A reason is required (3 to 200 characters)'), findsOneWidget);
      await tester.enterText(find.byKey(const ValueKey('inc-percent')), '60');
      await tester.tap(find.byKey(const ValueKey('inc-save')));
      await tester.pumpAndSettle();
      expect(find.text('At most 50%'), findsOneWidget);
      await tester.enterText(find.byKey(const ValueKey('inc-percent')), '10');
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('inc-preview')), findsOneWidget);
      expect(find.textContaining('₹44,000.00'), findsOneWidget);
    });

    testWidgets('direct structure create answered with APPROVAL_REQUIRED points to the increment flow', (tester) async {
      final repo = Fake4a()..createStructureError = const ApiException(code: 'APPROVAL_REQUIRED', message: 'x', status: 409);
      await pumpPayroll(tester, const StructureFormScreen(userId: 'u1'), repo: repo, perms: _manage);
      await tester.enterText(find.widgetWithText(TextFormField, 'Basic (monthly)'), '20000');
      await tester.tap(find.byKey(const ValueKey('save-structure')));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('approval-required')), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('go-increment')));
      await tester.pumpAndSettle();
      expect(find.text('New increment'), findsOneWidget);
    });

    testWidgets('split from gross follows the server split settings', (tester) async {
      final repo = Fake4a()
        ..settingsMap = {
          'payroll.split_basic_pct': 40,
          'payroll.split_hra_pct_of_basic': 50,
          'payroll.split_allowance_code': 'OTHERPAY',
          'payroll.split_allowance_name': 'Other pay',
        };
      await pumpPayroll(tester, const StructureFormScreen(userId: 'u1'), repo: repo, perms: _manage);
      await tester.enterText(find.byKey(const ValueKey('gross-target')), '10000');
      await tester.tap(find.byKey(const ValueKey('split-from-gross')));
      await tester.pumpAndSettle();
      String text(String label) =>
          tester.widget<TextFormField>(find.widgetWithText(TextFormField, label).first).controller!.text;
      expect(text('Basic (monthly)'), '4000.00');
      expect(text('HRA (monthly)'), '2000.00');
      expect(text('Code'), 'OTHERPAY');
    });

    testWidgets('salary history shows source chips and open increments', (tester) async {
      final repo = Fake4a()
        ..history = SalaryHistoryEntry.parseList({
          'items': [
            {'type': 'increment', 'increment_id': 'i1', 'effective_from': '2027-04-01', 'status': 'submitted', 'kind': 'percent', 'new_gross': '44000.00'},
            {'type': 'structure', 'structure_id': 's1', 'effective_from': '2026-04-01', 'monthly_gross': '40000.00', 'change_percent': '8.11', 'source': 'increment'},
          ],
        });
      await pumpPayroll(
        tester,
        const EmployeePayrollScreen(employee: EmployeeRef(id: 'u1', employeeId: 'EMP001', fullName: 'Asha Rao')),
        repo: repo,
        perms: _manage,
      );
      expect(find.byKey(const ValueKey('salary-history')), findsOneWidget);
      expect(find.textContaining('Increment waiting for approval'), findsOneWidget);
      expect(find.textContaining('Increment  -  +8.11%'), findsOneWidget);
      expect(find.byKey(const ValueKey('new-increment')), findsOneWidget);
    });
  });

  group('bulk hike wizard', () {
    testWidgets('validates, creates and lists skipped employees', (tester) async {
      final repo = Fake4a();
      await pumpPayroll(tester, const BulkHikeWizardScreen(),
        repo: repo,
        perms: _manage,
        extraOverrides: [departmentRefsProvider.overrideWith((ref) async => [const NamedRef(id: 'd1', name: 'Sales')])],);
      await tester.tap(find.byKey(const ValueKey('hike-create')));
      await tester.pumpAndSettle();
      expect(find.text('Percent is required'), findsOneWidget);
      expect(repo.hikeInput, isNull);
      await tester.enterText(find.byKey(const ValueKey('hike-percent')), '8');
      await tester.enterText(find.byKey(const ValueKey('hike-reason')), 'Annual increment');
      await tester.tap(find.byKey(const ValueKey('hike-create')));
      await tester.pumpAndSettle();
      expect(repo.hikeInput!.percent, '8.00');
      expect(repo.hikeInput!.effectiveFrom, '2026-11-01');
      expect(find.byKey(const ValueKey('hike-created')), findsOneWidget);
      expect(find.text('No salary structure'), findsOneWidget);
      expect(find.text('₹6,400.00'), findsOneWidget);
    });

    testWidgets('a server error stays on the form', (tester) async {
      final repo = Fake4a()..hikeError = const ApiException(code: 'TOO_MANY_ROWS', message: 'x', status: 422);
      await pumpPayroll(tester, const BulkHikeWizardScreen(),
        repo: repo,
        perms: _manage,
        extraOverrides: [departmentRefsProvider.overrideWith((ref) async => [const NamedRef(id: 'd1', name: 'Sales')])],);
      await tester.enterText(find.byKey(const ValueKey('hike-percent')), '8');
      await tester.enterText(find.byKey(const ValueKey('hike-reason')), 'Annual increment');
      await tester.tap(find.byKey(const ValueKey('hike-create')));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('hike-created')), findsNothing);
      expect(find.textContaining('500 employees'), findsOneWidget);
    });
  });

  group('CTC calculator', () {
    CtcResult result() => CtcResult.fromJson({
          'mode': 'from_gross',
          'frequency': 'monthly',
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
          'employee': {'pf': '1800.00', 'esi': '0.00', 'pt_annual_estimate': '2500.00', 'net_monthly_estimate': '37991.67'},
          'unallocated_monthly': '0.00',
          'flags': ['PF_CEILING_APPLIED'],
          'rules': <String, Object?>{},
        });

    testWidgets('validates the amount, then shows the breakdown', (tester) async {
      final repo = Fake4a()..ctc = result();
      await pumpPayroll(tester, const CtcCalculatorScreen(canManage: true), repo: repo, perms: _manage);
      await tester.tap(find.byKey(const ValueKey('ctc-calculate')));
      await tester.pumpAndSettle();
      expect(find.text('Amount is required'), findsOneWidget);
      await tester.enterText(find.byKey(const ValueKey('ctc-amount')), '40000');
      await tester.tap(find.byKey(const ValueKey('ctc-calculate')));
      await tester.pumpAndSettle();
      expect(find.text('₹42,761.54'), findsOneWidget);
      expect(find.text('₹5,13,138.48'), findsOneWidget);
      expect(find.text('₹37,991.67'), findsOneWidget);
      expect(find.text('PF is limited by the wage ceiling'), findsOneWidget);
      expect(find.byKey(const ValueKey('ctc-use')), findsOneWidget);
    });

    testWidgets('an error is shown and "Use as structure" needs payroll.manage', (tester) async {
      final repo = Fake4a()
        ..ctc = result()
        ..ctcError = const ApiException(code: 'VALIDATION_ERROR', message: 'x', status: 422);
      await pumpPayroll(tester, const CtcCalculatorScreen(), repo: repo, perms: const ['payroll.view_all']);
      await tester.enterText(find.byKey(const ValueKey('ctc-amount')), '40000');
      await tester.tap(find.byKey(const ValueKey('ctc-calculate')));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('ctc-error')), findsOneWidget);
      repo.ctcError = null;
      await tester.tap(find.byKey(const ValueKey('ctc-calculate')));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('ctc-use')), findsNothing);
    });
  });

  group('gratuity', () {
    testWidgets('shows totals, eligibility and flags; empty state', (tester) async {
      final repo = Fake4a()
        ..gratuityPage = GratuityPage.fromJson({
          'as_of': '2026-10-25',
          'basis': 'basic',
          'cap': '2000000.00',
          'min_years': 5,
          'items': [
            {
              'user': _emp,
              'join_date': '2019-06-01',
              'service': {'years': 7, 'months': 4, 'days': 24},
              'counted_years': 7,
              'eligible': true,
              'wage': '22000.00',
              'accrued': '88846.00',
              'monthly_accrual': '1057.69',
              'flags': ['CAPPED'],
            },
          ],
          'totals': {'accrued': '88846.00', 'eligible_accrued': '88846.00', 'monthly_accrual': '1057.69'},
          'page': 1,
          'page_size': 50,
          'total': 1,
        });
      await pumpPayroll(tester, const GratuityScreen(), repo: repo, perms: _manage);
      expect(find.text('Eligible'), findsOneWidget);
      expect(find.text('Capped at the legal limit'), findsOneWidget);
      expect(find.byKey(const ValueKey('gratuity-total')), findsOneWidget);
      expect(find.text('₹88,846.00'), findsWidgets);
      repo.gratuityPage = GratuityPage.fromJson({'as_of': '2026-10-25', 'items': <Object?>[], 'totals': <String, Object?>{}, 'total': 0});
      await tester.tap(find.byKey(const ValueKey('gratuity-eligible')));
      await tester.pumpAndSettle();
      expect(find.text('No employees match these filters.'), findsOneWidget);
    });
  });

  group('structure import wizard', () {
    final bytes = Uint8List.fromList([1, 2, 3]);
    PickedWorkbook file() => PickedWorkbook(name: 'structures.xlsx', bytes: bytes);

    StructureImportReport report({bool errors = false, String? sha}) => StructureImportReport.fromJson({
          'file_sha256': sha ?? file().sha256Hex,
          'rows': 3,
          'valid': errors ? 2 : 3,
          'errors': errors
              ? [
                  {'row': 14, 'sheet': 'Structures', 'field': 'employee_id', 'message': 'UNKNOWN_EMPLOYEE'},
                ]
              : <Object?>[],
          'warnings': [
            {'row': 2, 'sheet': 'Structures', 'field': 'employee_id', 'message': 'NEEDS_APPROVAL'},
          ],
          'actions': {'create': 1, 'increment': 2},
          'totals': {'monthly_gross': '120000.00'},
        });

    Future<void> checked(WidgetTester tester, Fake4a repo) async {
      await pumpPayroll(
        tester,
        const StructureImportWizardScreen(),
        repo: repo,
        perms: _manage,
        picker: FakePicker(file()),
      );
      await tester.tap(find.byKey(const ValueKey('simport-pick')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('simport-check')));
      await tester.pumpAndSettle();
    }

    testWidgets('template download works', (tester) async {
      final repo = Fake4a();
      await pumpPayroll(tester, const StructureImportWizardScreen(), repo: repo, perms: _manage);
      await tester.tap(find.byKey(const ValueKey('simport-template')));
      await tester.pumpAndSettle();
      expect(repo.calls, contains('structuresTemplate'));
    });

    testWidgets('errors per sheet and row block the import', (tester) async {
      final repo = Fake4a()..importReport = report(errors: true);
      await checked(tester, repo);
      expect(find.text('Structures row 14: Employee ID not found'), findsOneWidget);
      expect(find.byKey(const ValueKey('simport-actions')), findsOneWidget);
      final b = tester.widget<FilledButton>(find.byKey(const ValueKey('simport-commit')));
      expect(b.onPressed, isNull);
    });

    testWidgets('commit needs a reason and the password and sends the sha', (tester) async {
      final repo = Fake4a()..importReport = report();
      await checked(tester, repo);
      await tester.tap(find.byKey(const ValueKey('simport-commit')));
      await tester.pumpAndSettle();
      expect(find.text('A reason is required (3 to 200 characters)'), findsOneWidget);
      expect(repo.importCommits, isEmpty);
      await tester.enterText(find.widgetWithText(TextFormField, 'Reason for this import'), 'FY27 revision');
      await tester.tap(find.byKey(const ValueKey('simport-commit')));
      await tester.pumpAndSettle();
      await _password(tester, 'secret');
      expect(repo.importCommits.single, {'sha': file().sha256Hex, 'reason': 'FY27 revision', 'pw': 'secret'});
      expect(find.byKey(const ValueKey('simport-done')), findsOneWidget);
      expect(find.textContaining('2 increments were submitted'), findsOneWidget);
    });

    testWidgets('a changed file is refused locally', (tester) async {
      final repo = Fake4a()..importReport = report(sha: 'b' * 64);
      await checked(tester, repo);
      await tester.enterText(find.widgetWithText(TextFormField, 'Reason for this import'), 'FY27 revision');
      await tester.tap(find.byKey(const ValueKey('simport-commit')));
      await tester.pumpAndSettle();
      expect(repo.importCommits, isEmpty);
      expect(find.byKey(const ValueKey('simport-error')), findsOneWidget);
    });
  });
}
