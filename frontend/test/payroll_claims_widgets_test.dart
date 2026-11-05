import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/cache/json_cache.dart';
import 'package:employee_connect/features/admin/domain/admin_models.dart';
import 'package:employee_connect/features/admin/presentation/admin_widgets.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart' show Paged;
import 'package:employee_connect/features/leave/data/leave_repository.dart';
import 'package:employee_connect/features/leave/domain/leave_models.dart';
import 'package:employee_connect/features/leave/presentation/approvals_screen.dart';
import 'package:employee_connect/features/payroll/data/claim_bill_picker.dart';
import 'package:employee_connect/features/payroll/data/payroll_repository.dart';
import 'package:employee_connect/features/payroll/domain/claims_models.dart';
import 'package:employee_connect/features/payroll/domain/payroll_models.dart';
import 'package:employee_connect/features/payroll/presentation/claims_review.dart';
import 'package:employee_connect/features/payroll/presentation/claims_widgets.dart';
import 'package:employee_connect/features/payroll/presentation/my_claims_screen.dart';
import 'package:employee_connect/features/payroll/presentation/my_overtime_screen.dart';
import 'package:employee_connect/features/payroll/presentation/overtime_section.dart';
import 'package:employee_connect/features/payroll/presentation/pay_items_tab.dart';
import 'package:employee_connect/features/payroll/presentation/payroll_details_screen.dart';
import 'package:employee_connect/features/payroll/presentation/payroll_providers.dart';
import 'package:employee_connect/features/payroll/presentation/payroll_settings_tab.dart';
import 'package:employee_connect/features/payroll/presentation/payslip_edit_screen.dart';
import 'package:employee_connect/features/payroll/presentation/payslips_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/payroll_fakes.dart';

/// Widget tests for work package 3b (claims and overtime). Data comes from the
/// HAND-WRITTEN fixtures in `payroll_phase3b_contract_samples.json` (from
/// api-contract.md 17.6 and 17.8), not from captured responses.
const _own = ['payroll.view_own'];
const _ownClaims = ['payroll.view_own', 'reimbursement.submit'];
const _manage = ['payroll.view_all', 'payroll.manage'];
const _view = ['payroll.view_all'];

late Map<String, dynamic> samples;

Claim _claim(String key, {Map<String, dynamic> patch = const {}}) =>
    Claim.fromJson({...samples[key] as Map<String, dynamic>, ...patch});

OvertimeDay _ot(int i) => OvertimeDay.fromJson(
      ((samples['overtime_page'] as Map<String, dynamic>)['items'] as List)[i] as Map<String, dynamic>,
    );

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

/// Clears the tree so a second pump in the same test starts with fresh providers.
Future<void> _reset(WidgetTester t) async {
  await t.pumpWidget(const SizedBox());
}

class _EmptyLeaveRepo extends LeaveRepository {
  _EmptyLeaveRepo() : super(Dio(), MemoryJsonCache());

  @override
  Future<Paged<RequestItem>> adminRequests({
    required RequestKind kind,
    RequestStatus? status,
    int page = 1,
  }) async =>
      const Paged(items: [], page: 1, total: 0, pageSize: 20);
}

PickedBill _bill({String name = 'taxi.jpg', String? error}) =>
    PickedBill(name: name, bytes: Uint8List.fromList([1, 2, 3]), error: error);

void main() {
  setUpAll(() {
    samples = jsonDecode(File('test/fixtures/payroll_phase3b_contract_samples.json').readAsStringSync())
        as Map<String, dynamic>;
  });

  group('entry points in Payslips', () {
    testWidgets('Claims shows with reimbursement.submit only', (tester) async {
      await pumpPayroll(tester, const PayslipsScreen(), repo: FakeRepo(), perms: _ownClaims);
      expect(find.byKey(const ValueKey('open-my-claims')), findsOneWidget);
      await _reset(tester);
      await pumpPayroll(tester, const PayslipsScreen(), repo: FakeRepo(), perms: _own);
      expect(find.byKey(const ValueKey('open-my-claims')), findsNothing);
    });

    testWidgets('My overtime shows only when the server says enabled and eligible', (tester) async {
      await pumpPayroll(tester, const PayslipsScreen(), repo: FakeRepo(), perms: _own);
      expect(find.byKey(const ValueKey('open-my-overtime')), findsNothing);
      final on = FakeRepo()..myOvertimeData = MyOvertime.fromJson(samples['me_overtime'] as Map<String, dynamic>);
      await _reset(tester);
      await pumpPayroll(tester, const PayslipsScreen(), repo: on, perms: _own);
      expect(find.byKey(const ValueKey('open-my-overtime')), findsOneWidget);
      await _reset(tester);
      final notEligible = FakeRepo()
        ..myOvertimeData = const MyOvertime(enabled: true, items: []);
      await pumpPayroll(tester, const PayslipsScreen(), repo: notEligible, perms: _own);
      expect(find.byKey(const ValueKey('open-my-overtime')), findsNothing);
    });

    testWidgets('the Claims icon opens My claims', (tester) async {
      await pumpPayroll(tester, const PayslipsScreen(), repo: FakeRepo(), perms: _ownClaims);
      await _tapKey(tester, 'open-my-claims');
      expect(find.text('My claims'), findsWidgets);
      expect(find.text('You have no expense claims yet.'), findsOneWidget);
    });
  });

  group('My claims', () {
    FakeRepo withClaims() => FakeRepo()
      ..claimList = [_claim('claim'), _claim('claim_approved'), _claim('claim_rejected')];

    testWidgets('lists claims with amount and status chip', (tester) async {
      await pumpPayroll(tester, const MyClaimsScreen(), repo: withClaims(), perms: _ownClaims);
      expect(find.textContaining('Travel  -  ₹1,240.00'), findsOneWidget);
      expect(find.textContaining('Food  -  ₹800.00'), findsOneWidget);
      expect(find.text('Submitted'), findsWidgets);
      expect(find.text('Approved'), findsWidgets);
      expect(find.text('Rejected'), findsWidgets);
    });

    testWidgets('the status filter is sent to the server', (tester) async {
      final repo = withClaims();
      await pumpPayroll(tester, const MyClaimsScreen(), repo: repo, perms: _ownClaims);
      await _tapKey(tester, 'claims-filter-rejected');
      expect(repo.claimCalls.last, {'op': 'list', 'status': 'rejected'});
    });

    testWidgets('empty, error with retry, and no permission', (tester) async {
      await pumpPayroll(tester, const MyClaimsScreen(), repo: FakeRepo(), perms: _ownClaims);
      expect(find.text('You have no expense claims yet.'), findsOneWidget);
      await _reset(tester);
      final bad = FakeRepo()..claimsError = const ApiException(code: 'SERVICE_UNAVAILABLE', message: '', status: 503);
      await pumpPayroll(tester, const MyClaimsScreen(), repo: bad, perms: _ownClaims);
      expect(find.text('Try again'), findsOneWidget);
      await _reset(tester);
      await pumpPayroll(tester, const MyClaimsScreen(), repo: FakeRepo(), perms: _own);
      expect(find.text('You do not have permission to view this.'), findsOneWidget);
    });

    testWidgets('detail of an approved claim: approved amount, payout month, comment, no cancel', (tester) async {
      final repo = FakeRepo()..claimDetail = _claim('claim_approved');
      await pumpPayroll(tester, const ClaimDetailScreen(claimId: 'c2'), repo: repo, perms: _ownClaims);
      expect(find.text('₹800.00'), findsOneWidget);
      expect(find.text('₹700.00'), findsOneWidget);
      expect(find.text('November 2026 payroll'), findsOneWidget);
      expect(find.text('Approved, part is not claimable'), findsOneWidget);
      expect(find.textContaining('Meena Iyer'), findsOneWidget);
      expect(find.byKey(const ValueKey('cancel-claim')), findsNothing);
    });

    testWidgets('a PDF bill is fetched with the app client and handed to delivery', (tester) async {
      final repo = FakeRepo()..claimDetail = _claim('claim_approved');
      final delivery = FakeDelivery();
      await pumpPayroll(
        tester,
        const ClaimDetailScreen(claimId: 'c2'),
        repo: repo,
        perms: _ownClaims,
        delivery: delivery,
      );
      await _tapKey(tester, 'claim-view-bill');
      expect(repo.claimCalls.single, {'op': 'bill', 'url': '/api/files/f2222222-2222-2222-2222-222222222222'});
      expect(delivery.files.single.filename, startsWith('claim-bill-'));
      expect(delivery.files.single.filename, endsWith('.pdf'));
    });

    testWidgets('an image bill opens in a dialog; a failed fetch shows the error', (tester) async {
      final repo = FakeRepo()..claimDetail = _claim('claim');
      await pumpPayroll(tester, const ClaimDetailScreen(claimId: 'c1'), repo: repo, perms: _ownClaims);
      await _tapKey(tester, 'claim-view-bill');
      expect(find.byType(Dialog), findsOneWidget);
      await tester.tap(find.byTooltip('Close'));
      await tester.pumpAndSettle();
      repo.billError = const ApiException(code: 'NOT_FOUND', message: '', status: 404);
      await _tapKey(tester, 'claim-view-bill');
      expect(find.text('This payslip or record is not available.'), findsOneWidget);
    });

    testWidgets('a submitted claim can be cancelled after a confirmation', (tester) async {
      final repo = FakeRepo()..claimDetail = _claim('claim');
      await pumpPayroll(tester, const ClaimDetailScreen(claimId: 'c1'), repo: repo, perms: _ownClaims);
      await _tapKey(tester, 'cancel-claim');
      expect(repo.claimCalls, isEmpty); // not before the confirmation
      await tester.tap(find.widgetWithText(FilledButton, 'Cancel claim'));
      await tester.pumpAndSettle();
      expect(repo.claimCalls.single, {'op': 'cancel', 'id': 'c1111111-1111-1111-1111-111111111111'});
    });

    testWidgets('cancel on a claim that already moved on explains NOT_PENDING', (tester) async {
      final repo = FakeRepo()
        ..claimDetail = _claim('claim')
        ..decideError = const ApiException(code: 'NOT_PENDING', message: '', status: 409);
      await pumpPayroll(tester, const ClaimDetailScreen(claimId: 'c1'), repo: repo, perms: _ownClaims);
      await _tapKey(tester, 'cancel-claim');
      await tester.tap(find.widgetWithText(FilledButton, 'Cancel claim'));
      await tester.pumpAndSettle();
      expect(find.text('This item is no longer pending. Reload to see its current state.'), findsOneWidget);
    });

    testWidgets('verified claims can be cancelled too; paid ones cannot', (tester) async {
      final repo = FakeRepo()..claimDetail = _claim('claim', patch: {'status': 'verified'});
      await pumpPayroll(tester, const ClaimDetailScreen(claimId: 'c1'), repo: repo, perms: _ownClaims);
      expect(find.byKey(const ValueKey('cancel-claim')), findsOneWidget);
      await _reset(tester);
      repo.claimDetail = _claim('claim', patch: {'status': 'paid'});
      await pumpPayroll(tester, const ClaimDetailScreen(claimId: 'c1'), repo: repo, perms: _ownClaims);
      expect(find.byKey(const ValueKey('cancel-claim')), findsNothing);
    });
  });

  group('New claim form', () {
    Future<void> fill(
      WidgetTester t, {
      String category = 'Travel',
      String amount = '1240',
      String description = 'Cab to client site',
      bool date = true,
    }) async {
      await t.tap(find.byKey(const ValueKey('claim-category')));
      await t.pumpAndSettle();
      await t.tap(find.text(category).last);
      await t.pumpAndSettle();
      if (date) {
        await t.tap(find.byKey(const ValueKey('claim-date')));
        await t.pumpAndSettle();
        await t.tap(find.text('OK'));
        await t.pumpAndSettle();
      }
      await _enter(t, 'claim-amount', amount);
      await _enter(t, 'claim-description', description);
    }

    Future<void> addBill(WidgetTester t) async {
      await _tapKey(t, 'claim-add-bill');
      await t.tap(find.byKey(const ValueKey('bill-gallery')));
      await t.pumpAndSettle();
    }

    testWidgets('empty form: every field is validated and nothing is sent', (tester) async {
      final repo = FakeRepo();
      await pumpPayroll(tester, const ClaimFormScreen(), repo: repo, perms: _ownClaims);
      await _tapKey(tester, 'claim-submit');
      expect(find.text('Choose a category'), findsOneWidget);
      expect(find.text('Choose the expense date'), findsOneWidget);
      expect(find.text('Enter the amount'), findsOneWidget);
      expect(find.textContaining('Describe the expense'), findsOneWidget);
      expect(repo.claimCalls, isEmpty);
    });

    testWidgets('the chosen category shows its cap and whether a bill is needed', (tester) async {
      await pumpPayroll(tester, const ClaimFormScreen(), repo: FakeRepo(), perms: _ownClaims);
      await fill(tester, date: false, amount: '', description: '');
      expect(find.text('Limit ₹5,000.00  -  Bill required'), findsOneWidget);
    });

    testWidgets('a category that needs a bill refuses to submit without one', (tester) async {
      final repo = FakeRepo();
      await pumpPayroll(tester, const ClaimFormScreen(), repo: repo, perms: _ownClaims);
      await fill(tester);
      await _tapKey(tester, 'claim-submit');
      expect(find.text('A bill is required for Travel'), findsOneWidget);
      expect(repo.claimCalls, isEmpty);
    });

    testWidgets('an amount above the category cap is refused before sending', (tester) async {
      final repo = FakeRepo();
      await pumpPayroll(tester, const ClaimFormScreen(), repo: repo, perms: _ownClaims);
      await fill(tester, amount: '5000.01');
      await _tapKey(tester, 'claim-submit');
      expect(find.text('The limit for Travel is ₹5,000.00'), findsOneWidget);
      expect(repo.claimCalls, isEmpty);
    });

    testWidgets('a bill that is too large or the wrong type is refused with a message and not uploaded',
        (tester) async {
      final repo = FakeRepo();
      final picker = FakeBillPicker(_bill(name: 'huge.pdf', error: 'The file is too large (10 MB at most).'));
      await pumpPayroll(tester, const ClaimFormScreen(), repo: repo, perms: _ownClaims, billPicker: picker);
      await fill(tester);
      await addBill(tester);
      expect(picker.asked, [BillSource.gallery]);
      expect(find.text('The file is too large (10 MB at most).'), findsOneWidget);
      expect(find.byKey(const ValueKey('claim-bill-chosen')), findsNothing);
      await _tapKey(tester, 'claim-submit');
      expect(repo.claimCalls, isEmpty);
    });

    testWidgets('submit uploads the bill first, then creates the claim with its file id', (tester) async {
      final repo = FakeRepo();
      await pumpPayroll(
        tester,
        const ClaimFormScreen(),
        repo: repo,
        perms: _ownClaims,
        billPicker: FakeBillPicker(_bill()),
      );
      await fill(tester);
      await addBill(tester);
      expect(find.text('taxi.jpg'), findsOneWidget);
      await _tapKey(tester, 'claim-submit');
      expect(repo.claimCalls.map((c) => c['op']), ['upload', 'submit']);
      expect(repo.claimCalls[0], {'op': 'upload', 'name': 'taxi.jpg', 'len': 3});
      final s = repo.claimCalls[1];
      expect(s['category'], 'travel');
      expect(s['date'], '2026-10-25');
      expect(s['amount'], '1240.00');
      expect(s['description'], 'Cab to client site');
      expect(s['bill'], 'file-1');
    });

    testWidgets('a category without a required bill submits with bill_file_id null', (tester) async {
      final repo = FakeRepo();
      await pumpPayroll(tester, const ClaimFormScreen(), repo: repo, perms: _ownClaims);
      await fill(tester, category: 'Phone', amount: '399', description: 'Monthly recharge');
      await _tapKey(tester, 'claim-submit');
      expect(repo.claimCalls.single['op'], 'submit');
      expect(repo.claimCalls.single['bill'], isNull);
    });

    testWidgets('AMOUNT_ABOVE_LIMIT from the server shows the cap from details[0].message', (tester) async {
      final repo = FakeRepo()
        ..submitError = const ApiException(
          code: 'AMOUNT_ABOVE_LIMIT',
          message: 'x',
          status: 422,
          details: [FieldError('amount', '5000.00')],
        );
      await pumpPayroll(
        tester,
        const ClaimFormScreen(),
        repo: repo,
        perms: _ownClaims,
        billPicker: FakeBillPicker(_bill()),
      );
      await fill(tester, amount: '4000');
      await addBill(tester);
      await _tapKey(tester, 'claim-submit');
      expect(find.text('The amount is above the limit for this category (₹5,000.00).'), findsOneWidget);
    });

    testWidgets('DUPLICATE_BILL asks for another bill and drops the used one', (tester) async {
      final repo = FakeRepo()..submitError = const ApiException(code: 'DUPLICATE_BILL', message: '', status: 409);
      await pumpPayroll(
        tester,
        const ClaimFormScreen(),
        repo: repo,
        perms: _ownClaims,
        billPicker: FakeBillPicker(_bill()),
      );
      await fill(tester);
      await addBill(tester);
      await _tapKey(tester, 'claim-submit');
      expect(find.text('This bill was already used in another claim.'), findsOneWidget);
      expect(find.byKey(const ValueKey('claim-bill-chosen')), findsNothing);
      expect(find.byKey(const ValueKey('claim-add-bill')), findsOneWidget);
    });

    for (final c in [
      ('INVALID_CATEGORY', 'Choose a valid category.'),
      ('INVALID_DATE', 'Choose a date that is valid and not in the future'),
      ('BILL_REQUIRED', 'A bill is required for this category.'),
      ('CLAIM_LIMIT_REACHED', 'The monthly limit of claims has been reached.'),
      ('RATE_LIMITED', 'Too many requests'),
    ]) {
      testWidgets('server error ${c.$1} is explained and the form stays filled', (tester) async {
        final repo = FakeRepo()..submitError = ApiException(code: c.$1, message: '', status: 422);
        await pumpPayroll(tester, const ClaimFormScreen(), repo: repo, perms: _ownClaims);
        await fill(tester, category: 'Phone', amount: '399', description: 'Monthly recharge');
        await _tapKey(tester, 'claim-submit');
        expect(find.textContaining(c.$2), findsOneWidget);
        expect(find.text('Monthly recharge'), findsOneWidget);
      });
    }

    testWidgets('a failed upload stops before the claim is created', (tester) async {
      final repo = FakeRepo()
        ..uploadError = const ApiException(code: 'FILE_TYPE_NOT_ALLOWED', message: '', status: 422);
      await pumpPayroll(
        tester,
        const ClaimFormScreen(),
        repo: repo,
        perms: _ownClaims,
        billPicker: FakeBillPicker(_bill()),
      );
      await fill(tester);
      await addBill(tester);
      await _tapKey(tester, 'claim-submit');
      expect(repo.claimCalls.map((c) => c['op']), ['upload']);
      expect(find.byKey(const ValueKey('claim-error')), findsOneWidget);
    });

    testWidgets('loading categories can fail with a retry, and an empty list is explained', (tester) async {
      final bad = FakeRepo()..categoriesError = const ApiException(code: 'SERVICE_UNAVAILABLE', message: '', status: 503);
      await pumpPayroll(tester, const ClaimFormScreen(), repo: bad, perms: _ownClaims);
      expect(find.text('Try again'), findsOneWidget);
      await _reset(tester);
      final none = FakeRepo()..categories = const ClaimCategories(items: [], maxAgeDays: 60);
      await pumpPayroll(tester, const ClaimFormScreen(), repo: none, perms: _ownClaims);
      expect(find.textContaining('No claim categories'), findsOneWidget);
    });

    testWidgets('fits a small phone', (tester) async {
      await pumpPayroll(
        tester,
        const ClaimFormScreen(),
        repo: FakeRepo(),
        perms: _ownClaims,
        size: const Size(320, 640),
      );
      expect(tester.takeException(), isNull);
      expect(find.byKey(const ValueKey('claim-submit')), findsOneWidget);
    });
  });

  group('Approvals: Claims segment (manager)', () {
    Future<void> pumpApprovals(WidgetTester t, FakeRepo repo, List<String> perms) async {
      t.view.physicalSize = const Size(800, 2400);
      t.view.devicePixelRatio = 1;
      addTearDown(t.view.resetPhysicalSize);
      await t.pumpWidget(
        ProviderScope(
          overrides: [
            payrollRepositoryProvider.overrideWithValue(repo),
            leaveRepositoryProvider.overrideWithValue(_EmptyLeaveRepo()),
            adminPermissionsProvider.overrideWithValue(perms),
            adminSelfIdProvider.overrideWithValue('m1'),
            payrollClockProvider.overrideWithValue(() => DateTime(2026, 10, 25)),
          ],
          child: const MaterialApp(home: ApprovalsScreen()),
        ),
      );
      await t.pumpAndSettle();
    }

    Claim pending() => _claim('admin_claim_verified', patch: {'status': 'submitted', 'manager_step': 'pending'});

    testWidgets('the segment is offered only with a request permission', (tester) async {
      await pumpApprovals(tester, FakeRepo(), ['request.approve_team']);
      expect(find.text('Claims'), findsOneWidget);
      await _reset(tester);
      await pumpApprovals(tester, FakeRepo(), ['request.view_all']);
      expect(find.text('Claims'), findsNothing);
    });

    testWidgets('lists team claims of the manager stage; verify with an optional comment', (tester) async {
      final repo = FakeRepo()
        ..adminClaimList = [pending()]
        ..adminClaimDetail = pending();
      await pumpApprovals(tester, repo, ['request.approve_team']);
      await tester.tap(find.text('Claims'));
      await tester.pumpAndSettle();
      expect(repo.adminClaimQueries.last.stage, 'manager');
      expect(find.textContaining('Asha Rao'), findsOneWidget);
      await tester.tap(find.byType(ClaimTile));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('claim-view-bill')), findsOneWidget);
      await _tapKey(tester, 'claim-verify');
      await tester.tap(find.byKey(const ValueKey('claim-comment-ok')));
      await tester.pumpAndSettle();
      expect(repo.claimCalls.single, {'op': 'verify', 'id': 'c4444444-4444-4444-4444-444444444444', 'comment': ''});
    });

    testWidgets('reject needs a comment', (tester) async {
      final repo = FakeRepo()
        ..adminClaimList = [pending()]
        ..adminClaimDetail = pending();
      await pumpApprovals(tester, repo, ['request.approve_team']);
      await tester.tap(find.text('Claims'));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(ClaimTile));
      await tester.pumpAndSettle();
      await _tapKey(tester, 'claim-reject');
      await tester.tap(find.byKey(const ValueKey('claim-comment-ok')));
      await tester.pumpAndSettle();
      expect(find.text('A comment is required'), findsOneWidget);
      expect(repo.claimCalls, isEmpty);
      await _enter(tester, 'claim-comment', 'Not a business trip');
      await tester.tap(find.byKey(const ValueKey('claim-comment-ok')));
      await tester.pumpAndSettle();
      expect(repo.claimCalls.single, {
        'op': 'reject',
        'id': 'c4444444-4444-4444-4444-444444444444',
        'comment': 'Not a business trip',
      });
    });

    testWidgets('empty and error states of the segment', (tester) async {
      await pumpApprovals(tester, FakeRepo(), ['request.approve_team']);
      await tester.tap(find.text('Claims'));
      await tester.pumpAndSettle();
      expect(find.text('No claims waiting for your verification.'), findsOneWidget);
      await _reset(tester);
      final bad = FakeRepo()..claimsError = const ApiException(code: 'FORBIDDEN', message: '', status: 403);
      await pumpApprovals(tester, bad, ['request.approve_team']);
      await tester.tap(find.text('Claims'));
      await tester.pumpAndSettle();
      expect(find.text('Try again'), findsOneWidget);
    });

    testWidgets('a claim of the manager is not decided by the manager', (tester) async {
      final mine = _claim(
        'admin_claim_verified',
        patch: {
          'status': 'submitted',
          'manager_step': 'pending',
          'user': {'id': 'm1', 'employee_id': 'MGR001', 'full_name': 'Me'},
        },
      );
      final repo = FakeRepo()
        ..adminClaimList = [mine]
        ..adminClaimDetail = mine;
      await pumpApprovals(tester, repo, ['request.approve_team']);
      await tester.tap(find.text('Claims'));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(ClaimTile));
      await tester.pumpAndSettle();
      expect(find.text('You cannot decide on your own claim.'), findsOneWidget);
      expect(find.byKey(const ValueKey('claim-verify')), findsNothing);
      expect(find.byKey(const ValueKey('claim-reject')), findsNothing);
    });
  });

  group('HR claims', () {
    Claim verified() => _claim('admin_claim_verified');

    Future<FakeRepo> open(WidgetTester t, {Claim? claim, bool canManage = true, String? self}) async {
      final c = claim ?? verified();
      final repo = FakeRepo()
        ..adminClaimList = [c]
        ..adminClaimDetail = c;
      await pumpPayroll(
        t,
        Scaffold(body: ClaimsSection(canManage: canManage)),
        repo: repo,
        perms: canManage ? _manage : _view,
        selfId: self ?? 'me',
      );
      return repo;
    }

    Future<void> openTile(WidgetTester t) async {
      await t.tap(find.byType(ClaimTile));
      await t.pumpAndSettle();
    }

    testWidgets('the HR queue is stage=hr; All claims switches to stage=all', (tester) async {
      final repo = await open(tester);
      expect(repo.adminClaimQueries.last.stage, 'hr');
      await _tapKey(tester, 'claims-all');
      expect(repo.adminClaimQueries.last.stage, 'all');
      expect(find.byKey(const ValueKey('claims-status')), findsOneWidget);
    });

    testWidgets('approve sends amount, period, comment and the claim version', (tester) async {
      final repo = await open(tester);
      await openTile(tester);
      await _tapKey(tester, 'claim-approve');
      expect(find.text('Claimed ₹1,240.00'), findsOneWidget);
      await tester.enterText(find.widgetWithText(TextFormField, 'Approved amount'), '1000');
      await tester.tap(find.byKey(const ValueKey('claim-approve-ok')));
      await tester.pumpAndSettle();
      expect(repo.claimCalls.single, {
        'op': 'approve',
        'id': 'c4444444-4444-4444-4444-444444444444',
        'amount': '1000.00',
        'period': '2026-10',
        'v': 2,
        'comment': null,
      });
    });

    testWidgets('an approved amount above the claim is refused', (tester) async {
      final repo = await open(tester);
      await openTile(tester);
      await _tapKey(tester, 'claim-approve');
      await tester.enterText(find.widgetWithText(TextFormField, 'Approved amount'), '1240.01');
      await tester.tap(find.byKey(const ValueKey('claim-approve-ok')));
      await tester.pump();
      expect(find.textContaining('Cannot be above the amount claimed'), findsOneWidget);
      expect(repo.claimCalls, isEmpty);
    });

    for (final c in [
      ('SECOND_PERSON_REQUIRED', 'A different person must do this'),
      ('VERSION_CONFLICT', 'Someone else changed this record'),
      ('PERIOD_LOCKED', 'That month is locked'),
      ('ITEM_LOCKED', 'already in a payslip'),
      ('NOT_PENDING', 'no longer pending'),
    ]) {
      testWidgets('approve error ${c.$1} is explained', (tester) async {
        final repo = await open(tester);
        repo.decideError = ApiException(code: c.$1, message: '', status: 409);
        await openTile(tester);
        await _tapKey(tester, 'claim-approve');
        await tester.tap(find.byKey(const ValueKey('claim-approve-ok')));
        await tester.pumpAndSettle();
        expect(find.textContaining(c.$2), findsOneWidget);
        expect(repo.claimCalls.single['op'], 'approve');
      });
    }

    testWidgets('reject needs a comment', (tester) async {
      final repo = await open(tester);
      await openTile(tester);
      await _tapKey(tester, 'claim-reject');
      await _enter(tester, 'claim-comment', 'Duplicate of last month');
      await tester.tap(find.byKey(const ValueKey('claim-comment-ok')));
      await tester.pumpAndSettle();
      expect(repo.claimCalls.single['op'], 'reject');
      expect(repo.claimCalls.single['comment'], 'Duplicate of last month');
    });

    testWidgets('an approved claim can be moved to another payout month', (tester) async {
      final approved = _claim('admin_claim_verified', patch: {'status': 'approved', 'period': '2026-11', 'version': 3});
      final repo = await open(tester, claim: approved);
      await openTile(tester);
      expect(find.byKey(const ValueKey('claim-approve')), findsNothing);
      await _tapKey(tester, 'claim-payout');
      // same month: the button is disabled
      expect(tester.widget<FilledButton>(find.byKey(const ValueKey('claim-payout-ok'))).onPressed, isNull);
      await tester.tap(find.byType(DropdownButtonFormField<String>));
      await tester.pumpAndSettle();
      await tester.tap(find.text('December 2026').last);
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('claim-payout-ok')));
      await tester.pumpAndSettle();
      expect(repo.claimCalls.single, {
        'op': 'payout',
        'id': 'c4444444-4444-4444-4444-444444444444',
        'period': '2026-12',
        'v': 3,
      });
    });

    testWidgets('a claim already in a payslip shows the run hint and no actions', (tester) async {
      final p = _claim('admin_claim_processing');
      await open(tester, claim: p);
      await openTile(tester);
      expect(find.text('In payslip'), findsWidgets);
      expect(find.textContaining('will not be paid until moved'), findsOneWidget);
      expect(find.byKey(const ValueKey('claim-approve')), findsNothing);
      expect(find.byKey(const ValueKey('claim-payout')), findsNothing);
      expect(find.byKey(const ValueKey('claim-reject')), findsNothing);
    });

    testWidgets('a viewer without payroll.manage sees no actions', (tester) async {
      await open(tester, canManage: false);
      await openTile(tester);
      expect(find.byKey(const ValueKey('claim-approve')), findsNothing);
      expect(find.byKey(const ValueKey('claim-reject')), findsNothing);
    });

    testWidgets('nobody decides on their own claim', (tester) async {
      await open(tester, self: 'u1');
      await openTile(tester);
      expect(find.text('You cannot decide on your own claim.'), findsOneWidget);
      expect(find.byKey(const ValueKey('claim-approve')), findsNothing);
    });

    testWidgets('empty queue', (tester) async {
      await pumpPayroll(tester, const Scaffold(body: ClaimsSection(canManage: true)), repo: FakeRepo(), perms: _manage);
      expect(find.text('No claims waiting for HR.'), findsOneWidget);
    });
  });

  group('Overtime (HR)', () {
    FakeRepo withRows() => FakeRepo()
      ..overtimeList = [_ot(0), _ot(1), _ot(2)]
      ..overtimeSummaries = OvertimeSummary.parseList(samples['overtime_page'] as Map<String, dynamic>);

    Future<void> pumpOt(WidgetTester t, FakeRepo repo, {bool canManage = true, String? self}) =>
        pumpPayroll(
          t,
          Scaffold(body: OvertimeSection(canManage: canManage)),
          repo: repo,
          perms: canManage ? _manage : _view,
          selfId: self ?? 'me',
        );

    testWidgets('the Overtime segment shows only while the setting is on', (tester) async {
      await pumpPayroll(tester, const Scaffold(body: PayItemsTab()), repo: FakeRepo(), perms: _view);
      expect(find.byKey(const ValueKey('pay-section-claims')), findsOneWidget);
      expect(find.byKey(const ValueKey('pay-section-overtime')), findsNothing);
      await _reset(tester);
      final on = FakeRepo()..settingsMap = {'payroll.overtime_enabled': true};
      await pumpPayroll(tester, const Scaffold(body: PayItemsTab()), repo: on, perms: _view);
      expect(find.byKey(const ValueKey('pay-section-overtime')), findsOneWidget);
    });

    testWidgets('rows are grouped by employee with the month summary; flags and minutes show', (tester) async {
      await pumpOt(tester, withRows());
      expect(find.text('Asha Rao (EMP001)'), findsOneWidget);
      expect(find.text('Ravi Kumar (EMP002)'), findsOneWidget);
      expect(find.text('Approved 1h of 3h 30m computed, 1 to review'), findsOneWidget);
      expect(find.text('Computed 2h'), findsOneWidget);
      expect(find.text('Computed 1h 30m, approved 1h'), findsOneWidget);
      expect(find.text('Holiday or weekly off'), findsOneWidget);
      expect(find.text('Attendance changed after review'), findsOneWidget);
      expect(find.textContaining('₹'), findsNothing);
    });

    testWidgets('empty, error, and a viewer has no compute or selection', (tester) async {
      await pumpOt(tester, FakeRepo());
      expect(find.textContaining('No overtime for October 2026'), findsOneWidget);
      await _reset(tester);
      final bad = FakeRepo()..overtimeError = const ApiException(code: 'SERVICE_UNAVAILABLE', message: '', status: 503);
      await pumpOt(tester, bad);
      expect(find.text('Try again'), findsOneWidget);
      await _reset(tester);
      await pumpOt(tester, withRows(), canManage: false);
      expect(find.byKey(const ValueKey('ot-compute')), findsNothing);
      expect(find.byType(Checkbox), findsNothing);
    });

    testWidgets('Compute asks first, then computes the shown month', (tester) async {
      final repo = withRows();
      await pumpOt(tester, repo);
      await _tapKey(tester, 'ot-compute');
      expect(repo.overtimeCalls, isEmpty);
      await tester.tap(find.widgetWithText(FilledButton, 'Compute').last);
      await tester.pumpAndSettle();
      expect(repo.overtimeCalls.single, {'op': 'compute', 'period': '2026-10'});
      expect(find.textContaining('Computed: 3 new, 1 updated'), findsOneWidget);
    });

    testWidgets('OVERTIME_DISABLED on compute is explained', (tester) async {
      final repo = withRows()..overtimeError = null;
      await pumpOt(tester, repo);
      repo.overtimeError = const ApiException(code: 'OVERTIME_DISABLED', message: '', status: 409);
      await _tapKey(tester, 'ot-compute');
      await tester.tap(find.widgetWithText(FilledButton, 'Compute').last);
      await tester.pumpAndSettle();
      expect(find.text('Overtime is switched off in the payroll settings.'), findsOneWidget);
    });

    testWidgets('approve: minutes cannot exceed the computed minutes; sends version', (tester) async {
      final repo = withRows();
      await pumpOt(tester, repo);
      await _tapKey(tester, 'ot-menu-${_ot(0).id}');
      await tester.tap(find.text('Approve').last);
      await tester.pumpAndSettle();
      expect(tester.widget<TextFormField>(find.byKey(const ValueKey('ot-minutes'))).controller!.text, '120');
      await _enter(tester, 'ot-minutes', '121');
      await tester.tap(find.byKey(const ValueKey('ot-approve-ok')));
      await tester.pump();
      expect(find.textContaining('Cannot be above the computed'), findsOneWidget);
      expect(repo.overtimeCalls, isEmpty);
      await _enter(tester, 'ot-minutes', '90');
      await tester.tap(find.byKey(const ValueKey('ot-approve-ok')));
      await tester.pumpAndSettle();
      expect(repo.overtimeCalls.single, {
        'op': 'approve',
        'id': _ot(0).id,
        'minutes': 90,
        'v': 1,
        'comment': null,
      });
    });

    testWidgets('VERSION_CONFLICT and ITEM_LOCKED on approve are explained', (tester) async {
      final repo = withRows();
      await pumpOt(tester, repo);
      repo.overtimeError = const ApiException(code: 'VERSION_CONFLICT', message: '', status: 409);
      await _tapKey(tester, 'ot-menu-${_ot(0).id}');
      await tester.tap(find.text('Approve').last);
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('ot-approve-ok')));
      await tester.pumpAndSettle();
      expect(find.textContaining('Someone else changed this record'), findsOneWidget);
    });

    testWidgets('reject needs a comment', (tester) async {
      final repo = withRows();
      await pumpOt(tester, repo);
      await _tapKey(tester, 'ot-menu-${_ot(0).id}');
      await tester.tap(find.text('Reject').last);
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('claim-comment-ok')));
      await tester.pumpAndSettle();
      expect(find.text('A comment is required'), findsOneWidget);
      await _enter(tester, 'claim-comment', 'No attendance proof');
      await tester.tap(find.byKey(const ValueKey('claim-comment-ok')));
      await tester.pumpAndSettle();
      expect(repo.overtimeCalls.single, {'op': 'reject', 'id': _ot(0).id, 'comment': 'No attendance proof', 'v': 1});
    });

    testWidgets('bulk approve sends the selected ids and reports skipped rows', (tester) async {
      final repo = withRows()
        ..bulkResult = OvertimeBulkResult.fromJson(samples['overtime_bulk'] as Map<String, dynamic>);
      await pumpOt(tester, repo);
      expect(find.byKey(const ValueKey('ot-bulk-approve')), findsNothing);
      await _tapKey(tester, 'ot-check-${_ot(0).id}');
      await _tapKey(tester, 'ot-check-${_ot(2).id}');
      expect(find.text('Approve 2'), findsOneWidget);
      await _tapKey(tester, 'ot-bulk-approve');
      await tester.tap(find.widgetWithText(FilledButton, 'Approve').last);
      await tester.pumpAndSettle();
      expect(repo.overtimeCalls.single['op'], 'bulk');
      expect(repo.overtimeCalls.single['action'], 'approve');
      expect(repo.overtimeCalls.single['ids'], [_ot(0).id, _ot(2).id]);
      expect(find.textContaining('2 skipped: 1 already in a payslip, 1 your own overtime.'), findsOneWidget);
      expect(find.byKey(const ValueKey('ot-bulk-approve')), findsNothing); // selection cleared
    });

    testWidgets('bulk reject needs a comment', (tester) async {
      final repo = withRows();
      await pumpOt(tester, repo);
      await _tapKey(tester, 'ot-check-${_ot(0).id}');
      await _tapKey(tester, 'ot-bulk-reject');
      await _enter(tester, 'claim-comment', 'Duplicate entries');
      await tester.tap(find.byKey(const ValueKey('claim-comment-ok')));
      await tester.pumpAndSettle();
      expect(repo.overtimeCalls.single['action'], 'reject');
      expect(repo.overtimeCalls.single['comment'], 'Duplicate entries');
    });

    testWidgets('own overtime rows cannot be selected or reviewed', (tester) async {
      await pumpOt(tester, withRows(), self: 'u1');
      expect(find.byKey(ValueKey('ot-check-${_ot(0).id}')), findsNothing);
      expect(find.byKey(ValueKey('ot-menu-${_ot(0).id}')), findsNothing);
      expect(find.byKey(ValueKey('ot-check-${_ot(2).id}')), findsOneWidget);
    });
  });

  group('My overtime (employee)', () {
    testWidgets('shows day rows and the approved total, with no money', (tester) async {
      final repo = FakeRepo()..myOvertimeData = MyOvertime.fromJson(samples['me_overtime'] as Map<String, dynamic>);
      await pumpPayroll(tester, const MyOvertimeScreen(), repo: repo, perms: _own);
      expect(find.text('Approved this month: 1h 30m'), findsOneWidget);
      expect(find.text('Worked 2h extra, approved 1h 30m'), findsOneWidget);
      expect(find.text('To review'), findsOneWidget);
      expect(find.textContaining('₹'), findsNothing);
    });

    testWidgets('enabled false shows nothing', (tester) async {
      await pumpPayroll(tester, const MyOvertimeScreen(), repo: FakeRepo(), perms: _own);
      expect(find.text('Overtime is not available for you.'), findsOneWidget);
      expect(find.byType(Card), findsNothing);
    });

    testWidgets('month bar cannot go past this month', (tester) async {
      final repo = FakeRepo()..myOvertimeData = MyOvertime.fromJson(samples['me_overtime'] as Map<String, dynamic>);
      await pumpPayroll(tester, const MyOvertimeScreen(), repo: repo, perms: _own);
      expect(tester.widget<IconButton>(find.byKey(const ValueKey('my-ot-next'))).onPressed, isNull);
      await _tapKey(tester, 'my-ot-prev');
      expect(find.text('September 2026'), findsOneWidget);
    });
  });

  group('employee payroll details: overtime_eligible', () {
    const emp = EmployeeRef(id: 'u1', employeeId: 'EMP001', fullName: 'Asha Rao');

    PayrollDetails details({bool eligible = false}) => PayrollDetails(
          userId: 'u1',
          paymentMode: 'bank_transfer',
          includeInPayroll: true,
          revealed: false,
          overtimeEligible: eligible,
        );

    testWidgets('the switch saves on its own, with no password and no other field', (tester) async {
      final repo = FakeRepo();
      await pumpPayroll(
        tester,
        PayrollDetailsScreen(employee: emp, initial: details()),
        repo: repo,
        perms: _manage,
      );
      await _tapKey(tester, 'overtime-eligible');
      expect(repo.eligibleCalls.single, {'user': 'u1', 'eligible': true});
      expect(repo.savedDetails, isEmpty);
      expect(find.byKey(const ValueKey('reauth-password')), findsNothing);
      expect(tester.widget<SwitchListTile>(find.byKey(const ValueKey('overtime-eligible'))).value, isTrue);
    });

    testWidgets('a refused change flips the switch back and explains', (tester) async {
      final repo = FakeRepo()
        ..eligibleError = const ApiException(code: 'FORBIDDEN', message: '', status: 403);
      await pumpPayroll(
        tester,
        PayrollDetailsScreen(employee: emp, initial: details(eligible: true)),
        repo: repo,
        perms: _manage,
      );
      await _tapKey(tester, 'overtime-eligible');
      expect(repo.eligibleCalls.single['eligible'], isFalse);
      expect(tester.widget<SwitchListTile>(find.byKey(const ValueKey('overtime-eligible'))).value, isTrue);
      expect(find.textContaining('You are not allowed to do this'), findsOneWidget);
    });

    testWidgets('the main Save never carries overtime_eligible', (tester) async {
      final repo = FakeRepo();
      await pumpPayroll(
        tester,
        PayrollDetailsScreen(employee: emp, initial: details(eligible: true)),
        repo: repo,
        perms: _manage,
      );
      await tester.enterText(find.widgetWithText(TextFormField, 'Designation'), 'Senior Analyst');
      await _tapKey(tester, 'save-details');
      await tester.tap(find.widgetWithText(FilledButton, 'Continue'));
      // the Save button spins while the password prompt is open: pump, do not settle
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      await tester.enterText(find.byKey(const ValueKey('reauth-password')), 'secret');
      await tester.tap(find.text('Confirm'));
      await tester.pumpAndSettle();
      expect(repo.savedDetails.single.containsKey('overtime_eligible'), isFalse);
      expect(repo.savedDetails.single['designation'], 'Senior Analyst');
    });
  });

  group('settings: claims and overtime', () {
    Map<String, Object?> settings() => {
          'payroll.rounding': 'rupee',
          'payroll.reimbursement_categories': [
            {'code': 'travel', 'name': 'Travel', 'max_amount': '5000.00', 'bill_required': true},
            {'code': 'misc', 'name': 'Misc', 'max_amount': null, 'bill_required': false},
          ],
          'payroll.reimbursement_max_age_days': 60,
          'payroll.reimbursement_max_claims_per_month': 30,
          'payroll.overtime_enabled': false,
          'payroll.overtime_multiplier': 2,
          'payroll.overtime_month_days': 26,
          'payroll.overtime_min_minutes': 30,
          'payroll.overtime_rounding_minutes': 30,
          'payroll.overtime_max_hours_month': 50,
          'payroll.overtime_offday_full': true,
          'payroll.overtime_include_auto_closed': false,
        };

    Future<FakeRepo> openSettings(WidgetTester t, {List<String> perms = _manage}) async {
      final repo = FakeRepo()..settingsMap = settings();
      await pumpPayroll(t, const Scaffold(body: PayrollSettingsTab()), repo: repo, perms: perms);
      return repo;
    }

    testWidgets('categories and the claim and overtime fields are shown', (tester) async {
      await openSettings(tester);
      await tester.scrollUntilVisible(
        find.byKey(const ValueKey('cat-code-0')),
        300,
        scrollable: find.byType(Scrollable).first,
      );
      expect(tester.widget<TextField>(find.byKey(const ValueKey('cat-code-0'))).controller!.text, 'travel');
      expect(tester.widget<TextField>(find.byKey(const ValueKey('cat-max-0'))).controller!.text, '5000.00');
      expect(tester.widget<TextField>(find.byKey(const ValueKey('cat-max-1'))).controller!.text, '');
      expect(find.widgetWithText(TextFormField, 'Oldest expense that can be claimed'), findsOneWidget);
      expect(find.widgetWithText(TextFormField, 'Claims per employee per month'), findsOneWidget);
      expect(find.widgetWithText(SwitchListTile, 'Overtime is paid'), findsOneWidget);
      expect(find.widgetWithText(TextFormField, 'Overtime rate (times the hourly pay)'), findsOneWidget);
    });

    testWidgets('only changed keys are saved: an overtime switch and number', (tester) async {
      final repo = await openSettings(tester);
      final sw = find.widgetWithText(SwitchListTile, 'Overtime is paid');
      await tester.scrollUntilVisible(sw, 300, scrollable: find.byType(Scrollable).first);
      await tester.tap(sw);
      await tester.enterText(find.widgetWithText(TextFormField, 'Overtime rate (times the hourly pay)'), '1.5');
      await _tapKey(tester, 'save-settings');
      expect(repo.savedSettings.single, {'payroll.overtime_enabled': true, 'payroll.overtime_multiplier': 1.5});
    });

    testWidgets('out of range values are refused client-side', (tester) async {
      final repo = await openSettings(tester);
      await tester.scrollUntilVisible(
        find.widgetWithText(TextFormField, 'Overtime rate (times the hourly pay)'),
        300,
        scrollable: find.byType(Scrollable).first,
      );
      await tester.enterText(find.widgetWithText(TextFormField, 'Overtime rate (times the hourly pay)'), '3.5');
      await tester.enterText(find.widgetWithText(TextFormField, 'Days in a month for the hourly pay'), '19');
      await _tapKey(tester, 'save-settings');
      expect(find.text('At most 3'), findsOneWidget);
      expect(find.text('At least 20'), findsOneWidget);
      expect(repo.savedSettings, isEmpty);
    });

    testWidgets('editing a category saves the whole list with money as a string', (tester) async {
      final repo = await openSettings(tester);
      await tester.scrollUntilVisible(
        find.byKey(const ValueKey('cat-max-1')),
        300,
        scrollable: find.byType(Scrollable).first,
      );
      await tester.enterText(find.byKey(const ValueKey('cat-max-1')), '750');
      await tester.pump();
      await _tapKey(tester, 'save-settings');
      expect(repo.savedSettings.single, {
        'payroll.reimbursement_categories': [
          {'code': 'travel', 'name': 'Travel', 'max_amount': '5000.00', 'bill_required': true},
          {'code': 'misc', 'name': 'Misc', 'max_amount': '750.00', 'bill_required': false},
        ],
      });
    });

    testWidgets('add and remove a category; an invalid code blocks the save', (tester) async {
      final repo = await openSettings(tester);
      await _tapKey(tester, 'add-category');
      await tester.enterText(find.byKey(const ValueKey('cat-name-2')), 'Internet');
      await tester.enterText(find.byKey(const ValueKey('cat-code-2')), 'x');
      await tester.pump();
      await _tapKey(tester, 'save-settings');
      expect(find.textContaining('the code is 2 to 20 characters'), findsOneWidget);
      expect(repo.savedSettings, isEmpty);
      await tester.enterText(find.byKey(const ValueKey('cat-code-2')), 'internet');
      await tester.pump();
      await _tapKey(tester, 'cat-remove-1');
      await _tapKey(tester, 'save-settings');
      final saved = repo.savedSettings.single['payroll.reimbursement_categories']! as List;
      expect(saved.map((c) => (c as Map)['code']), ['travel', 'internet']);
      expect((saved.last as Map)['max_amount'], isNull);
    });

    testWidgets('a viewer cannot edit categories', (tester) async {
      await openSettings(tester, perms: _view);
      await tester.scrollUntilVisible(
        find.byKey(const ValueKey('cat-code-0')),
        300,
        scrollable: find.byType(Scrollable).first,
      );
      expect(find.byKey(const ValueKey('add-category')), findsNothing);
      expect(tester.widget<TextField>(find.byKey(const ValueKey('cat-code-0'))).enabled, isFalse);
    });
  });

  group('payslip: warnings and automatic lines', () {
    Payslip adminPayslip({List<String> warnings = const []}) {
      final j = payslipJson(admin: true, status: 'draft');
      j['inputs'] = {
        'tds': '1200.00',
        'pt_override': null,
        'adjustments': <Object?>[],
        'note': null,
        'components': [
          samples['overtime_component'],
          {
            'code': 'REIMBURSEMENT',
            'label': 'Reimbursement - Travel',
            'amount': '1240.00',
            'sources': [
              {'type': 'reimbursement', 'id': 'c1111111-1111-1111-1111-111111111111'},
            ],
          },
        ],
      };
      j['warnings'] = warnings;
      return Payslip.fromJson(j);
    }

    testWidgets('overtime shows minutes paid and approved; reimbursement shows its source', (tester) async {
      final repo = FakeRepo()..adminPayslip = adminPayslip();
      await pumpPayroll(tester, const PayslipEditScreen(payslipId: 'p1'), repo: repo, perms: _manage);
      expect(find.text('Overtime (7.5 h)'), findsOneWidget);
      expect(find.textContaining('7h 30m paid of 9h approved (capped)'), findsOneWidget);
      expect(find.text('Reimbursement - Travel'), findsOneWidget);
      expect(find.textContaining('Reimbursement c1111111'), findsOneWidget);
      expect(find.text('₹1,240.00'), findsOneWidget);
    });

    testWidgets('OVERTIME_UNREVIEWED and OVERTIME_CAPPED are readable chips', (tester) async {
      final repo = FakeRepo()..adminPayslip = adminPayslip(warnings: ['OVERTIME_UNREVIEWED', 'OVERTIME_CAPPED']);
      await pumpPayroll(tester, const PayslipEditScreen(payslipId: 'p1'), repo: repo, perms: _manage);
      expect(find.text('Overtime not reviewed'), findsOneWidget);
      expect(find.text('Overtime capped'), findsOneWidget);
    });
  });
}
