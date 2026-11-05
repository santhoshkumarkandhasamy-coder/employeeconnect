import 'dart:typed_data';

import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/admin/domain/admin_models.dart' show EmployeeRef;
import 'package:employee_connect/features/attendance/domain/attendance_models.dart' show Paged;
import 'package:employee_connect/features/payroll/data/payroll_repository.dart';
import 'package:employee_connect/features/payroll/domain/payroll_models.dart';
import 'package:employee_connect/features/payroll/domain/phase4a_models.dart' show Increment, IncrementBatch, IncrementQuery;
import 'package:employee_connect/features/payroll/domain/phase4b_models.dart';
import 'package:employee_connect/features/payroll/presentation/approvals_tab.dart';
import 'package:employee_connect/features/payroll/presentation/exits_section.dart';
import 'package:employee_connect/features/payroll/presentation/fnf_detail_screen.dart';
import 'package:employee_connect/features/payroll/presentation/fnf_editor_screen.dart';
import 'package:employee_connect/features/payroll/presentation/payslips_screen.dart';
import 'package:employee_connect/features/payroll/presentation/record_exit_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/payroll_fakes.dart';
import 'payroll_phase4b_logic_test.dart' show fnfJson;

Map<String, dynamic> _listItem({String id = 'f1', String status = 'submitted'}) => {
      'id': id,
      'user': {'id': 'u1', 'employee_id': 'EMP001', 'full_name': 'Asha Rao'},
      'status': status,
      'last_working_day': '2026-12-15',
      'settlement_period': '2026-12',
      'exit_reason': 'resignation',
      'run_status': 'none',
      'warnings': ['CLAIMS_OPEN'],
    };

Map<String, dynamic> _preview({String net = '47900.00'}) => {
      'estimate': true,
      'days': {'basis_days': 31.0, 'employed_days': 15.0, 'lop_days': 0.0, 'paid_days': 15.0},
      'earnings': [
        {'code': 'BASIC', 'label': 'Basic', 'amount': '52000.00'},
      ],
      'deductions': [
        {'code': 'PF', 'label': 'PF', 'amount': '4100.00'},
      ],
      'totals': {'gross_earnings': '52000.00', 'total_deductions': '4100.00', 'net_pay': net},
      'warnings': <String>[],
      'source': 'structure',
    };

class Fake4b extends FakeRepo {
  Fnf current = Fnf.fromJson(fnfJson());
  FnfPreview preview = FnfPreview.fromJson(_preview());
  List<FnfListItem> list = [];
  ApiException? approveError;
  final calls4b = <String>[];
  final approvals = <Map<String, Object?>>[];
  final rejects = <Map<String, Object?>>[];
  final revokes = <Map<String, Object?>>[];
  final updates = <Map<String, Object?>>[];
  final creates = <Map<String, Object?>>[];
  MyFnf? myFnfValue;

  @override
  Future<Paged<Increment>> increments(IncrementQuery q, {int page = 1}) async =>
      const Paged(items: [], page: 1, total: 0, pageSize: 20);

  @override
  Future<Paged<IncrementBatch>> incrementBatches({String? kind, int page = 1}) async =>
      const Paged(items: [], page: 1, total: 0, pageSize: 20);

  @override
  Future<Paged<FnfListItem>> fnfList(FnfQuery q, {int page = 1}) async {
    final items = [for (final i in list) if (q.status == null || i.status == q.status) i];
    return Paged(items: items, page: 1, total: items.length, pageSize: 20);
  }

  @override
  Future<Fnf> fnf(String id) async => current;

  @override
  Future<FnfPreview> fnfPreview(String id) async => preview;

  @override
  Future<Fnf> submitFnf(String id, {required int expectedVersion}) async {
    calls4b.add('submit:$expectedVersion');
    return current;
  }

  @override
  Future<Fnf> approveFnf(
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
    return Fnf.fromJson(fnfJson(status: 'approved'));
  }

  @override
  Future<Fnf> rejectFnf(String id, {required int expectedVersion, required String comment}) async {
    rejects.add({'v': expectedVersion, 'comment': comment});
    return Fnf.fromJson(fnfJson());
  }

  @override
  Future<Fnf> revokeFnf(
    String id, {
    required int expectedVersion,
    required String reason,
    required String currentPassword,
  }) async {
    revokes.add({'v': expectedVersion, 'reason': reason, 'pw': currentPassword});
    return Fnf.fromJson(fnfJson());
  }

  @override
  Future<Fnf> updateFnf(String id, FnfEdit edit, {required int expectedVersion}) async {
    updates.add(edit.toJson(expectedVersion));
    return current;
  }

  @override
  Future<Fnf> createFnf(String userId, Map<String, Object?> body) async {
    creates.add(body);
    return current;
  }

  @override
  Future<PayrollFile> fnfStatement(String id) async {
    calls4b.add('statement');
    return PayrollFile(Uint8List.fromList([37, 80, 68, 70]), 'fnf-EMP001-2026-12.pdf');
  }

  @override
  Future<MyFnf?> myFnf() async => myFnfValue;

  @override
  Future<PayrollFile> myFnfStatement() async {
    calls4b.add('myStatement');
    return PayrollFile(Uint8List.fromList([37, 80, 68, 70]), 'fnf-EMP001-2026-12.pdf');
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

Fnf _f(String status, {String createdBy = 'maker', String? submittedBy, List<String> warnings = const []}) =>
    Fnf.fromJson(fnfJson(status: status, createdBy: createdBy, submittedBy: submittedBy, warnings: warnings));

void main() {
  group('F&F detail', () {
    testWidgets('draft: sections, estimate, checklist, maker actions, no approve', (tester) async {
      final repo = Fake4b()..current = _f('draft', warnings: ['CLAIMS_OPEN']);
      await pumpPayroll(tester, const FnfDetailScreen(fnfId: 'f1'), repo: repo, perms: _manage);
      expect(find.text('Notice period'), findsOneWidget);
      expect(find.textContaining('Earned leave 2026: 6 days'), findsOneWidget);
      expect(find.textContaining('L-0007'), findsOneWidget);
      expect(find.textContaining('Pro-rata bonus'), findsOneWidget);
      expect(find.text('₹47,900.00'), findsOneWidget);
      expect(find.text('Reimbursement claims are still open'), findsOneWidget);
      expect(find.byKey(const ValueKey('fnf-submit')), findsOneWidget);
      expect(find.byKey(const ValueKey('fnf-edit')), findsOneWidget);
      expect(find.byKey(const ValueKey('fnf-approve')), findsNothing);
      await tester.tap(find.byKey(const ValueKey('fnf-submit')));
      await tester.pumpAndSettle();
      expect(repo.calls4b, ['submit:3']);
    });

    testWidgets('negative settlement blocks submit and approve', (tester) async {
      final repo = Fake4b()
        ..current = _f('draft', warnings: ['NEGATIVE_NET_ESTIMATE'])
        ..preview = FnfPreview.fromJson(_preview(net: '-1200.00'));
      await pumpPayroll(tester, const FnfDetailScreen(fnfId: 'f1'), repo: repo, perms: _manage);
      expect(find.byKey(const ValueKey('fnf-negative')), findsOneWidget);
      expect(tester.widget<FilledButton>(find.byKey(const ValueKey('fnf-submit'))).onPressed, isNull);
    });

    testWidgets('negative settlement blocks approve', (tester) async {
      final repo = Fake4b()
        ..preview = FnfPreview.fromJson(_preview(net: '-1200.00'));
      repo.current = _f('submitted', submittedBy: 'maker', warnings: ['NEGATIVE_NET_ESTIMATE']);
      await pumpPayroll(tester, const FnfDetailScreen(fnfId: 'f1'), repo: repo, perms: _approve);
      final approve = find.byKey(const ValueKey('fnf-approve'));
      expect(approve, findsOneWidget);
      expect(tester.widget<ButtonStyleButton>(approve).onPressed, isNull);
    });

    testWidgets('approved is read-only: no edit, no submit, revoke offered to a checker', (tester) async {
      final repo = Fake4b()..current = _f('approved', submittedBy: 'sub');
      await pumpPayroll(tester, const FnfDetailScreen(fnfId: 'f1'), repo: repo, perms: [..._manage, ..._approve]);
      expect(find.byKey(const ValueKey('fnf-readonly')), findsOneWidget);
      expect(find.byKey(const ValueKey('fnf-edit')), findsNothing);
      expect(find.byKey(const ValueKey('fnf-submit')), findsNothing);
      expect(find.byKey(const ValueKey('fnf-revoke')), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('fnf-revoke')));
      await tester.pumpAndSettle();
      await _comment(tester, 'Wrong LWD');
      await _password(tester, 'secret');
      expect(repo.revokes.single, {'v': 3, 'reason': 'Wrong LWD', 'pw': 'secret'});
    });

    testWidgets('approve asks for a comment then the password and sends the version', (tester) async {
      final repo = Fake4b()..current = _f('submitted', submittedBy: 'sub');
      await pumpPayroll(tester, const FnfDetailScreen(fnfId: 'f1'), repo: repo, perms: _approve);
      await tester.tap(find.byKey(const ValueKey('fnf-approve')));
      await tester.pumpAndSettle();
      await _comment(tester, 'looks fine');
      await _password(tester, 'secret');
      expect(repo.approvals.single, {'v': 3, 'pw': 'secret', 'comment': 'looks fine'});
    });

    testWidgets('a wrong password asks again', (tester) async {
      final repo = Fake4b()
        ..current = _f('submitted', submittedBy: 'sub')
        ..pwRejects = 1;
      await pumpPayroll(tester, const FnfDetailScreen(fnfId: 'f1'), repo: repo, perms: _approve);
      await tester.tap(find.byKey(const ValueKey('fnf-approve')));
      await tester.pumpAndSettle();
      await _comment(tester, '');
      await _password(tester, 'bad');
      expect(find.text('That password is not correct.'), findsOneWidget);
      await _password(tester, 'good');
      expect(repo.approvals.length, 2);
    });

    testWidgets('creator, submitter, contributor and the employee see no approve / revoke', (tester) async {
      for (final self in ['maker', 'sub', 'u1']) {
        final repo = Fake4b()..current = _f('submitted', submittedBy: 'sub');
        await pumpPayroll(tester, const FnfDetailScreen(fnfId: 'f1'), repo: repo, perms: _approve, selfId: self);
        expect(find.byKey(const ValueKey('fnf-approve')), findsNothing, reason: self);
        expect(find.byKey(const ValueKey('cannot-decide')), findsOneWidget, reason: self);
      }
      final repo = Fake4b()..current = Fnf.fromJson({...fnfJson(status: 'approved'), 'contributors': ['c9']});
      await pumpPayroll(tester, const FnfDetailScreen(fnfId: 'f1'), repo: repo, perms: _approve, selfId: 'c9');
      expect(find.byKey(const ValueKey('fnf-revoke')), findsNothing);
    });

    testWidgets('server errors are mapped: SECOND_PERSON_REQUIRED and VERSION_CONFLICT', (tester) async {
      final repo = Fake4b()
        ..current = _f('submitted', submittedBy: 'sub')
        ..approveError = const ApiException(code: 'SECOND_PERSON_REQUIRED', message: 'x', status: 403);
      await pumpPayroll(tester, const FnfDetailScreen(fnfId: 'f1'), repo: repo, perms: _approve);
      await tester.tap(find.byKey(const ValueKey('fnf-approve')));
      await tester.pumpAndSettle();
      await _comment(tester, '');
      await _password(tester, 'pw');
      expect(find.textContaining('different person'), findsOneWidget);

      repo.approveError = const ApiException(code: 'VERSION_CONFLICT', message: 'x', status: 409);
      await tester.tap(find.byKey(const ValueKey('fnf-approve')));
      await tester.pumpAndSettle();
      await _comment(tester, '');
      await _password(tester, 'pw');
      expect(find.textContaining('Someone else changed this record'), findsOneWidget);
    });

    testWidgets('reject needs a comment', (tester) async {
      final repo = Fake4b()..current = _f('submitted', submittedBy: 'sub');
      await pumpPayroll(tester, const FnfDetailScreen(fnfId: 'f1'), repo: repo, perms: _approve);
      await tester.tap(find.byKey(const ValueKey('fnf-reject')));
      await tester.pumpAndSettle();
      await _comment(tester, '');
      expect(find.textContaining('A comment is required'), findsOneWidget);
      expect(repo.rejects, isEmpty);
      await _comment(tester, 'Dates wrong');
      expect(repo.rejects.single, {'v': 3, 'comment': 'Dates wrong'});
    });

    testWidgets('statement download hands the PDF to the delivery', (tester) async {
      final repo = Fake4b();
      final delivery = FakeDelivery();
      await pumpPayroll(tester, const FnfDetailScreen(fnfId: 'f1'), repo: repo, perms: _manage, delivery: delivery);
      await tester.tap(find.byKey(const ValueKey('fnf-statement')));
      await tester.pumpAndSettle();
      expect(repo.calls4b, contains('statement'));
      expect(delivery.files.single.filename, 'fnf-EMP001-2026-12.pdf');
    });

    testWidgets('load error shows retry', (tester) async {
      final repo = _Failing();
      await pumpPayroll(tester, const FnfDetailScreen(fnfId: 'f1'), repo: repo, perms: _manage);
      expect(find.text('Try again'), findsOneWidget);
    });
  });

  group('F&F editor', () {
    testWidgets('an approved settlement cannot be edited', (tester) async {
      await pumpPayroll(tester, FnfEditorScreen(fnf: _f('approved')), repo: Fake4b(), perms: _manage);
      expect(find.byKey(const ValueKey('editor-readonly')), findsOneWidget);
      expect(find.byKey(const ValueKey('editor-save')), findsNothing);
    });

    testWidgets('saves every section whole with the version', (tester) async {
      final repo = Fake4b();
      await pumpPayroll(tester, FnfEditorScreen(fnf: _f('draft')), repo: repo, perms: _manage);
      // loan: pay outside payroll
      await tester.ensureVisible(find.byKey(const ValueKey('loan-mode-0')));
      await tester.tap(find.byKey(const ValueKey('loan-mode-0')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Recover in the final payslip').last);
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.byKey(const ValueKey('editor-save')));
      await tester.tap(find.byKey(const ValueKey('editor-save')));
      await tester.pumpAndSettle();
      final body = repo.updates.single;
      expect(body['expected_version'], 3);
      expect(body['last_working_day'], '2026-12-15');
      expect(body['settlement_period'], '2026-12');
      expect(body['encashments'], [
        {'leave_type_id': 'lt1', 'year': 2026, 'days': 6.0},
      ]);
      expect(body['loans'], [
        {'loan_id': 'l1', 'mode': 'payroll'},
      ]);
      expect(body['items'], [
        {'code': 'BONUS', 'label': 'Pro-rata bonus', 'amount': '3000.00'},
      ]);
      expect(body['move_open_items'], true);
      expect((body['notice']! as Map)['waived'], false);
    });

    testWidgets('an override needs a reason', (tester) async {
      final repo = Fake4b();
      await pumpPayroll(tester, FnfEditorScreen(fnf: _f('draft')), repo: repo, perms: _manage);
      await tester.ensureVisible(find.byKey(const ValueKey('notice-override-switch')));
      await tester.tap(find.byKey(const ValueKey('notice-override-switch')));
      await tester.pumpAndSettle();
      await tester.enterText(find.byKey(const ValueKey('notice-override-days')), '5');
      await tester.ensureVisible(find.byKey(const ValueKey('editor-save')));
      await tester.tap(find.byKey(const ValueKey('editor-save')));
      await tester.pumpAndSettle();
      expect(repo.updates, isEmpty);
      expect(find.textContaining('is required'), findsWidgets);
      await tester.enterText(find.byKey(const ValueKey('notice-override-reason')), 'Handover completed early');
      await tester.ensureVisible(find.byKey(const ValueKey('editor-save')));
      await tester.tap(find.byKey(const ValueKey('editor-save')));
      await tester.pumpAndSettle();
      final notice = repo.updates.single['notice']! as Map;
      expect(notice['override'], {'days': 5, 'reason': 'Handover completed early'});
    });

    testWidgets('an outside loan needs a date and a reference', (tester) async {
      final repo = Fake4b();
      await pumpPayroll(tester, FnfEditorScreen(fnf: _f('draft')), repo: repo, perms: _manage);
      await tester.ensureVisible(find.byKey(const ValueKey('loan-mode-0')));
      await tester.tap(find.byKey(const ValueKey('loan-mode-0')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Paid outside payroll').last);
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.byKey(const ValueKey('editor-save')));
      await tester.tap(find.byKey(const ValueKey('editor-save')));
      await tester.pumpAndSettle();
      expect(repo.updates, isEmpty);
      expect(find.text('The payment reference is required'), findsOneWidget);
    });

    testWidgets('a version conflict is explained', (tester) async {
      final repo = _ConflictOnUpdate();
      await pumpPayroll(tester, FnfEditorScreen(fnf: _f('draft')), repo: repo, perms: _manage);
      await tester.ensureVisible(find.byKey(const ValueKey('editor-save')));
      await tester.tap(find.byKey(const ValueKey('editor-save')));
      await tester.pumpAndSettle();
      expect(find.textContaining('Someone else changed this record'), findsOneWidget);
    });
  });

  group('record exit', () {
    testWidgets('validates the dates then creates the draft', (tester) async {
      final repo = Fake4b();
      await pumpPayroll(
        tester,
        const RecordExitScreen(employee: EmployeeRef(id: 'u1', employeeId: 'EMP001', fullName: 'Asha Rao')),
        repo: repo,
        perms: _manage,
      );
      await tester.tap(find.byKey(const ValueKey('exit-save')));
      await tester.pumpAndSettle();
      expect(repo.creates, isEmpty);
      expect(find.text('Choose the last working day'), findsOneWidget);
    });

    testWidgets('sends the wire body and opens the settlement', (tester) async {
      final repo = Fake4b();
      await pumpPayroll(
        tester,
        RecordExitScreen(
          employee: const EmployeeRef(id: 'u1', employeeId: 'EMP001', fullName: 'Asha Rao'),
          initial: ExitValues(
            lastWorkingDay: DateTime(2026, 12, 15),
            resignationDate: DateTime(2026, 11, 15),
            exitReason: 'resignation',
            settlementPeriod: '2026-12',
          ),
        ),
        repo: repo,
        perms: _manage,
      );
      await tester.tap(find.byKey(const ValueKey('exit-save')));
      await tester.pumpAndSettle();
      expect(repo.creates.single, {
        'resignation_date': '2026-11-15',
        'last_working_day': '2026-12-15',
        'exit_reason': 'resignation',
        'notice_period_days': null,
        'settlement_period': '2026-12',
        'remarks': null,
      });
      expect(find.text('Final settlement'), findsOneWidget);
    });

    testWidgets('FNF_EXISTS is explained', (tester) async {
      final repo = _ExistsOnCreate();
      await pumpPayroll(
        tester,
        RecordExitScreen(
          employee: const EmployeeRef(id: 'u1', employeeId: 'EMP001', fullName: 'Asha Rao'),
          initial: ExitValues(
            lastWorkingDay: DateTime(2026, 12, 15),
            exitReason: 'other',
            settlementPeriod: '2026-12',
          ),
        ),
        repo: repo,
        perms: _manage,
      );
      await tester.tap(find.byKey(const ValueKey('exit-save')));
      await tester.pumpAndSettle();
      expect(find.textContaining('already has a final settlement'), findsOneWidget);
    });
  });

  group('lists and approvals', () {
    testWidgets('the approvals queue lists submitted settlements and opens one', (tester) async {
      final repo = Fake4b()
        ..list = [FnfListItem.fromJson(_listItem()), FnfListItem.fromJson(_listItem(id: 'f2', status: 'draft'))]
        ..current = _f('submitted', submittedBy: 'sub');
      await pumpPayroll(tester, const Scaffold(body: ApprovalsTab()), repo: repo, perms: _approve);
      expect(find.byKey(const ValueKey('fnf-f1')), findsOneWidget);
      expect(find.byKey(const ValueKey('fnf-f2')), findsNothing);
      await tester.tap(find.byKey(const ValueKey('fnf-f1')));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('fnf-approve')), findsOneWidget);
    });

    testWidgets('exits list: empty state and Record exit for managers', (tester) async {
      final repo = Fake4b();
      await pumpPayroll(tester, const Scaffold(body: ExitsSection(canManage: true)), repo: repo, perms: _manage);
      expect(find.textContaining('No final settlements yet'), findsOneWidget);
      expect(find.byKey(const ValueKey('record-exit')), findsOneWidget);
    });

    testWidgets('exits list shows rows and hides Record exit without manage', (tester) async {
      final repo = Fake4b();
      repo.list = [FnfListItem.fromJson(_listItem(status: 'draft'))];
      await pumpPayroll(tester, const Scaffold(body: ExitsSection(canManage: false)), repo: repo, perms: _manage);
      expect(find.byKey(const ValueKey('fnf-f1')), findsOneWidget);
      expect(find.text('Draft'), findsWidgets);
      expect(find.byKey(const ValueKey('record-exit')), findsNothing);
    });

    testWidgets('exits list shows the load error with retry', (tester) async {
      await pumpPayroll(tester, const Scaffold(body: ExitsSection(canManage: true)), repo: _Failing(), perms: _manage);
      expect(find.text('Try again'), findsWidgets);
    });
  });

  group('employee side', () {
    testWidgets('no card when there is no settlement', (tester) async {
      await pumpPayroll(tester, const PayslipsScreen(), repo: Fake4b(), perms: const ['payroll.view_own']);
      expect(find.byKey(const ValueKey('my-fnf-card')), findsNothing);
    });

    testWidgets('in progress: no money, no download', (tester) async {
      final repo = Fake4b()
        ..myFnfValue = const MyFnf(id: 'f1', status: 'in_progress', lastWorkingDay: '2026-12-15', settlementPeriod: '2026-12');
      await pumpPayroll(tester, const PayslipsScreen(), repo: repo, perms: const ['payroll.view_own']);
      expect(find.byKey(const ValueKey('my-fnf-card')), findsOneWidget);
      expect(find.text('In progress'), findsOneWidget);
      expect(find.byKey(const ValueKey('my-fnf-statement')), findsNothing);
    });

    testWidgets('settled: downloads the statement', (tester) async {
      final repo = Fake4b()
        ..myFnfValue = const MyFnf(
          id: 'f1',
          status: 'settled',
          lastWorkingDay: '2026-12-15',
          settlementPeriod: '2026-12',
          finalPayslipId: 'p9',
        );
      final delivery = FakeDelivery();
      await pumpPayroll(
        tester,
        const PayslipsScreen(),
        repo: repo,
        perms: const ['payroll.view_own'],
        delivery: delivery,
      );
      expect(find.text('Settled'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('my-fnf-statement')));
      await tester.pumpAndSettle();
      expect(repo.calls4b, ['myStatement']);
      expect(delivery.files, hasLength(1));
    });

    testWidgets('without payroll.view_own the card (and screen) is not shown', (tester) async {
      final repo = Fake4b()
        ..myFnfValue = const MyFnf(id: 'f1', status: 'settled', lastWorkingDay: '2026-12-15', settlementPeriod: '2026-12');
      await pumpPayroll(tester, const PayslipsScreen(), repo: repo, perms: const []);
      expect(find.byKey(const ValueKey('my-fnf-card')), findsNothing);
    });

    testWidgets('payslip list marks the final payslip', (tester) async {
      final repo = Fake4b();
      await pumpPayroll(
        tester,
        PayslipTile(
          item: PayslipListItem.fromJson({'id': 'p', 'period': '2026-12', 'revision': 1, 'is_final': true}),
        ),
        repo: repo,
      );
      expect(find.byKey(const ValueKey('final-chip')), findsOneWidget);
    });
  });
}

class _Failing extends Fake4b {
  @override
  Future<Fnf> fnf(String id) async => throw const ApiException(code: 'NETWORK_ERROR', message: 'offline');

  @override
  Future<Paged<FnfListItem>> fnfList(FnfQuery q, {int page = 1}) async =>
      throw const ApiException(code: 'NETWORK_ERROR', message: 'offline');
}

class _ConflictOnUpdate extends Fake4b {
  @override
  Future<Fnf> updateFnf(String id, FnfEdit edit, {required int expectedVersion}) async =>
      throw const ApiException(code: 'VERSION_CONFLICT', message: 'x', status: 409);
}

class _ExistsOnCreate extends Fake4b {
  @override
  Future<Fnf> createFnf(String userId, Map<String, Object?> body) async =>
      throw const ApiException(code: 'FNF_EXISTS', message: 'x', status: 409);
}
