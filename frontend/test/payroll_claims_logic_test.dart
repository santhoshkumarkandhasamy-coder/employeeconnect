import 'dart:convert';
import 'dart:io';

import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/leave/domain/leave_logic.dart';
import 'package:employee_connect/features/payroll/domain/claims_models.dart';
import 'package:employee_connect/features/payroll/domain/pay_items_models.dart';
import 'package:employee_connect/features/payroll/domain/payroll_logic.dart';
import 'package:employee_connect/features/payroll/domain/payroll_models.dart';
import 'package:employee_connect/features/payroll/presentation/claims_review.dart';
import 'package:employee_connect/features/payroll/presentation/my_claims_screen.dart';
import 'package:flutter_test/flutter_test.dart';

/// Pure logic for work package 3b (claims, overtime). Parsing uses the
/// HAND-WRITTEN fixtures in `payroll_phase3b_contract_samples.json` (from
/// api-contract.md 17.6 and 17.8), not captured responses.
late Map<String, dynamic> samples;

ApiException _api(String key) {
  final env = (samples['errors'] as Map<String, dynamic>)[key] as Map<String, dynamic>;
  final e = env['error'] as Map<String, dynamic>;
  return ApiException(
    code: e['code'] as String,
    message: e['message'] as String,
    details: [
      for (final d in (e['details'] as List).cast<Map<String, dynamic>>())
        FieldError(d['field'] as String, d['message'] as String),
    ],
  );
}

void main() {
  setUpAll(() {
    samples = jsonDecode(File('test/fixtures/payroll_phase3b_contract_samples.json').readAsStringSync())
        as Map<String, dynamic>;
  });

  group('parsing', () {
    test('categories keep caps as strings and the max age', () {
      final c = ClaimCategories.fromJson(samples['categories'] as Map<String, dynamic>);
      expect(c.items, hasLength(3));
      expect(c.byCode('travel')!.maxAmount, '5000.00');
      expect(c.byCode('food')!.maxAmount, isNull);
      expect(c.byCode('phone')!.billRequired, isFalse);
      expect(c.maxAgeDays, 60);
    });

    test('a submitted claim', () {
      final c = Claim.fromJson(samples['claim'] as Map<String, dynamic>);
      expect(c.status, 'submitted');
      expect(c.amount, '1240.00');
      expect(c.approvedAmount, isNull);
      expect(c.bill!.isPdf, isFalse);
      expect(c.canCancel, isTrue);
      expect(c.awaitingManager, isTrue);
      expect(c.awaitingHr, isFalse);
      expect(c.period, isNull);
    });

    test('an approved claim carries amount, payout month, people and comment', () {
      final c = Claim.fromJson(samples['claim_approved'] as Map<String, dynamic>);
      expect(c.approvedAmount, '700.00');
      expect(c.periodText, 'November 2026');
      expect(c.verifier!.fullName, 'Meena Iyer');
      expect(c.decisionComment, 'Approved, part is not claimable');
      expect(c.bill!.isPdf, isTrue);
      expect(c.canCancel, isFalse);
      expect(c.isApproved, isTrue);
    });

    test('admin claims: user, run status, flags, processing', () {
      final v = Claim.fromJson(samples['admin_claim_verified'] as Map<String, dynamic>);
      expect(v.user!.employeeId, 'EMP001');
      expect(v.awaitingHr, isTrue);
      final p = Claim.fromJson(samples['admin_claim_processing'] as Map<String, dynamic>);
      expect(p.status, 'processing');
      expect(p.flags, ['PERIOD_CLOSED']);
      expect(p.payslipId, 'p1');
      expect(claimStatusLabel(p.status), 'In payslip');
      expect(p.canCancel, isFalse);
    });

    test('a claim with no bill and a manager step skipped is in the HR queue', () {
      final c = Claim.fromJson(samples['claim_rejected'] as Map<String, dynamic>);
      expect(c.bill, isNull);
      expect(c.managerStep, 'skipped');
      expect(c.status, 'rejected');
      final skipped = Claim.fromJson({...samples['claim'] as Map<String, dynamic>, 'manager_step': 'skipped'});
      expect(skipped.awaitingHr, isTrue);
      expect(skipped.awaitingManager, isFalse);
    });

    test('overtime list rows and summary', () {
      final page = samples['overtime_page'] as Map<String, dynamic>;
      final rows = [for (final r in (page['items'] as List).cast<Map<String, dynamic>>()) OvertimeDay.fromJson(r)];
      expect(rows.map((r) => r.computedMinutes), [120, 90, 60]);
      expect(rows[0].flags, ['OFF_DAY']);
      expect(rows[0].approvedMinutes, isNull);
      expect(rows[1].approvedMinutes, 60);
      expect(rows[1].reviewer!.employeeId, 'HR001');
      expect(rows[0].reviewable, isTrue);
      final sum = OvertimeSummary.parseList(page);
      expect(sum.first.userId, 'u1');
      expect(sum.first.approvedMinutes, 60);
      expect(sum.first.pendingRows, 1);
    });

    test('/me/overtime enabled and disabled', () {
      final on = MyOvertime.fromJson(samples['me_overtime'] as Map<String, dynamic>);
      expect(on.visible, isTrue);
      expect(on.items, hasLength(2));
      expect(on.totalApprovedMinutes, 90);
      final off = MyOvertime.fromJson(samples['me_overtime_disabled'] as Map<String, dynamic>);
      expect(off.enabled, isFalse);
      expect(off.visible, isFalse);
      expect(off.items, isEmpty);
      expect(
        MyOvertime.fromJson({'enabled': true, 'eligible': false, 'items': <Object?>[]}).visible,
        isFalse,
      );
    });

    test('compute and bulk results', () {
      final c = OvertimeComputeResult.fromJson(samples['overtime_compute'] as Map<String, dynamic>);
      expect((c.created, c.updated, c.flaggedChanged), (34, 5, 1));
      final b = OvertimeBulkResult.fromJson(samples['overtime_bulk'] as Map<String, dynamic>);
      expect(b.updated, 2);
      expect(b.skipped.map((s) => s.reason), ['ITEM_LOCKED', 'FORBIDDEN']);
      expect(b.message, '2 rows updated. 2 skipped: 1 already in a payslip, 1 your own overtime.');
      expect(const OvertimeBulkResult(updated: 1, skipped: []).message, '1 row updated.');
    });

    test('payroll details carry overtime_eligible (default false)', () {
      expect(PayrollDetails.fromJson(samples['details_eligible'] as Map<String, dynamic>).overtimeEligible, isTrue);
      expect(PayrollDetails.fromJson({'user_id': 'u'}).overtimeEligible, isFalse);
    });

    test('overtime component: minutes after and before the cap', () {
      final c = PayslipComponent.fromJson(samples['overtime_component'] as Map<String, dynamic>);
      expect(c.minutes, 450);
      expect(c.minutesTotal, 540);
      expect(c.minutesCaption, '7h 30m paid of 9h approved (capped)');
      expect(c.sources.single.type, 'overtime');
      expect(
        PayslipComponent.fromJson({'code': 'OVERTIME', 'amount': '1.00', 'minutes': 60, 'minutes_total': 60})
            .minutesCaption,
        '1h paid',
      );
      expect(PayslipComponent.fromJson({'code': 'BONUS', 'amount': '1.00'}).minutesCaption, isNull);
    });
  });

  group('claim validation', () {
    const travel = ClaimCategory(code: 'travel', name: 'Travel', maxAmount: '5000.00');

    test('amount: required, money format, above zero, category cap', () {
      expect(validateClaimAmount('', travel), isNotNull);
      expect(validateClaimAmount('abc', travel), isNotNull);
      expect(validateClaimAmount('0', travel), isNotNull);
      expect(validateClaimAmount('12.345', travel), isNotNull);
      expect(validateClaimAmount('1240.50', travel), isNull);
      expect(validateClaimAmount('5000', travel), isNull);
      expect(validateClaimAmount('5000.01', travel), contains('₹5,000.00'));
      expect(validateClaimAmount('999999', const ClaimCategory(code: 'x', name: 'X')), isNull);
    });

    test('description is 3 to 300 characters', () {
      expect(validateClaimDescription('ab'), isNotNull);
      expect(validateClaimDescription('   '), isNotNull);
      expect(validateClaimDescription('Cab'), isNull);
      expect(validateClaimDescription('x' * 301), isNotNull);
    });

    test('expense date: not future, not older than the max age', () {
      final today = DateTime(2026, 10, 25);
      expect(validateExpenseDate(null, today, 60), isNotNull);
      expect(validateExpenseDate(DateTime(2026, 10, 26), today, 60), contains('future'));
      expect(validateExpenseDate(DateTime(2026, 10, 25), today, 60), isNull);
      expect(validateExpenseDate(DateTime(2026, 8, 26), today, 60), isNull); // exactly 60 days
      expect(validateExpenseDate(DateTime(2026, 8, 25), today, 60), contains('60 days'));
    });

    test('bill: type and size are judged from the name and reported size', () {
      expect(validateClaimBill(name: 'bill.PDF', size: 1000), isNull);
      expect(validateClaimBill(name: 'a.jpeg', size: claimBillMaxBytes), isNull);
      expect(validateClaimBill(name: 'a.png', size: claimBillMaxBytes + 1), contains('too large'));
      expect(validateClaimBill(name: 'a.gif', size: 10), contains('JPEG, PNG or PDF'));
      expect(validateClaimBill(name: 'noext', size: 10), isNotNull);
      expect(validateClaimBill(name: 'a.pdf', size: 0), contains('empty'));
      expect(billContentTypeFor('x.pdf'), 'application/pdf');
      expect(billContentTypeFor('x.PNG'), 'image/png');
      expect(billContentTypeFor('x.jpg'), 'image/jpeg');
    });

    test('approved amount: above zero and not above the claim', () {
      expect(validateApprovedAmount('', '800.00'), isNotNull);
      expect(validateApprovedAmount('0', '800.00'), isNotNull);
      expect(validateApprovedAmount('800.01', '800.00'), contains('₹800.00'));
      expect(validateApprovedAmount('800', '800.00'), isNull);
      expect(validateApprovedAmount('700.50', '800.00'), isNull);
    });

    test('comments: reject needs 3 to 200 characters, approve may be empty', () {
      expect(validateClaimComment('', required: true), isNotNull);
      expect(validateClaimComment('no', required: true), isNotNull);
      expect(validateClaimComment('Not a business expense', required: true), isNull);
      expect(validateClaimComment('', required: false), isNull);
      expect(validateClaimComment('x' * 201, required: false), isNotNull);
    });

    test('approved minutes: whole, 0 to computed', () {
      expect(validateApprovedMinutes('', 120), isNotNull);
      expect(validateApprovedMinutes('1.5', 120), isNotNull);
      expect(validateApprovedMinutes('121', 120), contains('120'));
      expect(validateApprovedMinutes('0', 120), isNull);
      expect(validateApprovedMinutes('120', 120), isNull);
      expect(minutesText(450), '7h 30m');
    });
  });

  group('category settings', () {
    test('round trip keeps money as a string and null for no cap', () {
      final l = CategoryDraft.parseList([
        {'code': 'travel', 'name': 'Travel', 'max_amount': '5000.00', 'bill_required': true},
        {'code': 'misc', 'name': 'Misc', 'max_amount': null, 'bill_required': false},
      ]);
      expect(l.map((c) => c.toJson()).toList(), [
        {'code': 'travel', 'name': 'Travel', 'max_amount': '5000.00', 'bill_required': true},
        {'code': 'misc', 'name': 'Misc', 'max_amount': null, 'bill_required': false},
      ]);
      expect(const CategoryDraft(code: 'a1', name: 'A', maxAmount: '250').toJson()['max_amount'], '250.00');
    });

    test('validation: code pattern, unique codes, name length, limit, 20 at most', () {
      CategoryDraft c(String code, {String name = 'N', String? max}) =>
          CategoryDraft(code: code, name: name, maxAmount: max);
      expect(validateCategories([c('travel')]), isNull);
      expect(validateCategories([c('Travel')]), contains('code'));
      expect(validateCategories([c('1abc')]), contains('code'));
      expect(validateCategories([c('a')]), contains('code'));
      expect(validateCategories([c('travel'), c('travel')]), contains('twice'));
      expect(validateCategories([c('travel', name: '')]), contains('name'));
      expect(validateCategories([c('travel', name: 'x' * 41)]), contains('name'));
      expect(validateCategories([c('travel', max: '0')]), contains('limit'));
      expect(validateCategories([c('travel', max: 'abc')]), contains('limit'));
      expect(validateCategories([for (var i = 0; i < 21; i++) c('cat$i')]), contains('20'));
      expect(validateCategories(const []), isNull);
    });

    test('the new settings keys are specified with the contract ranges', () {
      final byKey = {for (final s in payrollSettingSpecs) s.key: s};
      expect(byKey['payroll.overtime_enabled']!.kind, PayrollSettingKind.boolean);
      expect(byKey['payroll.overtime_multiplier']!.min, 1);
      expect(byKey['payroll.overtime_multiplier']!.max, 3);
      expect(byKey['payroll.overtime_month_days']!.min, 20);
      expect(byKey['payroll.overtime_month_days']!.max, 31);
      expect(byKey['payroll.overtime_min_minutes']!.max, 240);
      expect(byKey['payroll.overtime_rounding_minutes']!.min, 1);
      expect(byKey['payroll.overtime_max_hours_month']!.max, 300);
      expect(byKey['payroll.reimbursement_max_age_days']!.max, 365);
      expect(byKey['payroll.reimbursement_max_claims_per_month']!.max, 200);
      expect(byKey['payroll.overtime_offday_full']!.kind, PayrollSettingKind.boolean);
      expect(byKey['payroll.overtime_include_auto_closed']!.kind, PayrollSettingKind.boolean);
      expect(reimbursementCategoriesKey, 'payroll.reimbursement_categories');
    });
  });

  group('error texts', () {
    test('every new code has its own text', () {
      String m(String code) => payrollErrorMessage(ApiException(code: code, message: ''));
      expect(m('INVALID_CATEGORY'), contains('category'));
      expect(m('BILL_REQUIRED'), contains('bill'));
      expect(m('DUPLICATE_BILL'), contains('already used'));
      expect(m('CLAIM_LIMIT_REACHED'), contains('limit'));
      expect(m('OVERTIME_DISABLED'), contains('switched off'));
      expect(m('NOT_PENDING'), contains('no longer pending'));
      expect(m('VERSION_CONFLICT'), contains('Reload'));
      expect(m('PERIOD_LOCKED'), contains('locked'));
      expect(m('ITEM_LOCKED'), contains('payslip'));
      expect(m('SECOND_PERSON_REQUIRED'), contains('different person'));
      expect(m('SECOND_PERSON_REQUIRED'), contains('approve'));
      expect(m('INVALID_DATE'), contains('date'));
    });

    test('AMOUNT_ABOVE_LIMIT shows the category cap from details[0].message', () {
      final e = _api('AMOUNT_ABOVE_LIMIT');
      expect(claimCapFromError(e), '5000.00');
      expect(payrollErrorMessage(e), 'The amount is above the limit for this category (₹5,000.00).');
    });

    test('AMOUNT_ABOVE_LIMIT on approved_amount does not treat the text as a cap', () {
      final e = _api('AMOUNT_ABOVE_LIMIT_APPROVE');
      expect(claimCapFromError(e), isNull);
      expect(payrollErrorMessage(e), contains('cannot be above the amount claimed'));
      expect(
        payrollErrorMessage(const ApiException(code: 'AMOUNT_ABOVE_LIMIT', message: '')),
        'The amount is above the limit for this category.',
      );
    });
  });

  group('permissions and push routing', () {
    test('claims need reimbursement.submit; the manager step needs a request permission', () {
      expect(canSubmitClaims(['reimbursement.submit']), isTrue);
      expect(canSubmitClaims(['payroll.view_own']), isFalse);
      expect(canSeeTeamClaims(['request.view_team']), isTrue);
      expect(canSeeTeamClaims(['request.approve_all']), isTrue);
      expect(canSeeTeamClaims(['payroll.view_all']), isFalse);
    });

    test('push types map to claim screens, with the claim id', () {
      final s = parseRequestPush({'type': 'reimbursement_submitted', 'id': 'c1'})!;
      expect(s.screen, RequestPushScreen.claimApprovals);
      expect(s.id, 'c1');
      final d = parseRequestPush({'type': 'reimbursement_decision', 'id': 'c2'})!;
      expect(d.screen, RequestPushScreen.myClaims);
      expect(d.id, 'c2');
      expect(parseRequestPush({'type': 'loan_created', 'id': 'x'}), isNull);
      // the existing mappings are untouched
      expect(parseRequestPush({'type': 'new_request'})!.screen, RequestPushScreen.approvals);
    });

    test('claimPushScreen picks the screen by permission', () {
      final decision = parseRequestPush({'type': 'reimbursement_decision', 'id': 'c2'})!;
      expect(claimPushScreen(decision, ['reimbursement.submit']), isA<ClaimDetailScreen>());
      expect(claimPushScreen(decision, ['payroll.view_own']), isNull);
      final noId = parseRequestPush({'type': 'reimbursement_decision'})!;
      expect(claimPushScreen(noId, ['reimbursement.submit']), isA<MyClaimsScreen>());

      final submitted = parseRequestPush({'type': 'reimbursement_submitted', 'id': 'c1'})!;
      final mgr = claimPushScreen(submitted, ['request.approve_team']) as ClaimReviewScreen;
      expect(mgr.claimId, 'c1');
      expect(mgr.canManage, isFalse);
      final hr = claimPushScreen(submitted, ['request.approve_all', 'payroll.view_all', 'payroll.manage'])
          as ClaimReviewScreen;
      expect(hr.canManage, isTrue);
      expect(claimPushScreen(submitted, ['payroll.view_own']), isNull);
      expect(claimPushScreen(const RequestPushTarget(RequestPushScreen.approvals), ['request.approve_team']), isNull);
    });
  });
}
