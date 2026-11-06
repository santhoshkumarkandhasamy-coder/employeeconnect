import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/payroll/domain/payroll_logic.dart';
import 'package:employee_connect/features/payroll/domain/payroll_models.dart';
import 'package:employee_connect/features/payroll/domain/phase5a_models.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/payroll_fakes.dart' show payslipJson, runJson;

Map<String, dynamic> userJson(String id, [String name = 'Asha Rao']) =>
    {'id': id, 'employee_id': 'EMP-$id', 'full_name': name};

Map<String, dynamic> requestJson({
  String id = 'pr1',
  String status = 'submitted',
  String scope = 'run',
  String requester = 'maker',
  int version = 1,
  String? staleReason,
  List<String>? makers,
  bool totals = true,
}) =>
    {
      'id': id,
      'scope': scope,
      'run': {'id': 'r1', 'period': '2026-10', 'status': 'processed'},
      'payslip': scope == 'payslip'
          ? {'id': 'p1', 'user': userJson('emp', 'Ravi Kumar'), 'revision': 2}
          : null,
      'status': status,
      'stale_reason': staleReason,
      'snapshot': {
        'payslips': 46,
        'held': 2,
        'warnings': 5,
        'processed_at': '2026-10-30T09:00:00Z',
        if (totals)
          'totals': {
            'gross_earnings': '1850000.00',
            'total_deductions': '160000.00',
            'net_pay': '1690000.00',
            'employer_cost': '0.00',
          },
      },
      'comment': null,
      'requested_by': userJson(requester, 'Meena'),
      'requested_at': '2026-10-30T10:00:00Z',
      'expires_at': '2026-11-02T10:00:00Z',
      'makers': makers == null ? null : [for (final m in makers) userJson(m)],
      'decided_by': null,
      'decided_at': null,
      'decision_comment': null,
      'version': version,
    };

Map<String, dynamic> checkJson({
  bool makerChecker = true,
  bool canRequest = true,
  bool canApprove = false,
  Map<String, dynamic>? open,
  List<String> me = const [],
}) =>
    {
      'maker_checker': makerChecker,
      'can_request': canRequest,
      'can_publish_directly': !makerChecker,
      'can_approve': canApprove,
      'open_request': open,
      'me': {'reasons': me},
      'makers': [
        {'user': userJson('maker', 'Meena'), 'reasons': ['PROCESSED_RUN']},
      ],
      'publishable': {'payslips': 46, 'held': 2},
    };

Map<String, dynamic> lineJson({
  String id = 'l1',
  int seq = 1,
  String user = 'u1',
  List<String> flags = const [],
  String source = 'snapshot',
  String status = 'pending',
  bool changed = false,
}) =>
    {
      'id': id,
      'seq': seq,
      'user': userJson(user),
      'payslip': {'id': 'p$seq', 'revision': 1},
      'amount': '35300.00',
      'beneficiary_name': 'A B',
      'account': 'XXXXXX1234',
      'ifsc': 'HDFC0001234',
      'account_source': source,
      'current': changed ? {'account': 'XXXXXX9876', 'ifsc': 'ICIC0000456'} : null,
      'flags': changed ? ['BANK_DETAILS_CHANGED', ...flags] : flags,
      'status': status,
      'reference': status == 'paid' ? 'XXXX7788' : null,
      'paid_on': status == 'paid' ? '2026-10-31' : null,
      'version': 1,
    };

Map<String, dynamic> batchJson({
  String id = 'b1',
  String status = 'draft',
  String creator = 'maker',
  String? submitter,
  String? decider,
  int version = 1,
  List<Map<String, dynamic>>? lines,
  List<Map<String, dynamic>> blockers = const [],
  String? exportedAt,
}) =>
    {
      'id': id,
      'reference': 'PAY-2026-10-01',
      'run': {'id': 'r1', 'period': '2026-10'},
      'status': status,
      'debit_account': {'id': 'ba1', 'label': 'HDFC salary account', 'account': 'XXXXXX4321'},
      'value_date': '2026-10-31',
      'format': 'generic_neft_csv',
      'counts': {'lines': 2, 'excluded': 0, 'exported': status == 'exported' ? 2 : 0, 'paid': 0, 'failed': 0},
      'total': '70600.00',
      'lines': lines ?? [lineJson(), lineJson(id: 'l2', seq: 2, user: 'u2')],
      'blockers': blockers,
      'file_sha256': status == 'exported' ? 'a' * 64 : null,
      'exported_at': exportedAt ?? (status == 'exported' ? '2026-10-30T09:00:00Z' : null),
      'exported_by': status == 'exported' ? userJson('maker') : null,
      'created_by': userJson(creator, 'Meena'),
      'created_at': '2026-10-29T09:00:00Z',
      'submitted_by': submitter == null ? null : userJson(submitter),
      'submitted_at': submitter == null ? null : '2026-10-29T10:00:00Z',
      'decided_by': decider == null ? null : userJson(decider),
      'decided_at': null,
      'decision_comment': null,
      'version': version,
    };

void main() {
  group('publish request models', () {
    test('parses a request with totals and stale reason', () {
      final r = PublishRequest.fromJson(requestJson(status: 'stale', staleReason: 'RUN_REOPENED'));
      expect(r.id, 'pr1');
      expect(r.isSubmitted, isFalse);
      expect(r.staleReason, 'RUN_REOPENED');
      expect(r.snapshot.payslips, 46);
      expect(r.snapshot.totals!.netPay, '1690000.00');
      expect(r.checkId, 'r1');
      expect(r.makers, isNull);
      expect(publishStaleLabel(r.staleReason), contains('reopened'));
      expect(publishStatusLabel('stale'), 'Out of date');
    });

    test('a list item has no totals; a payslip request checks the payslip id', () {
      final r = PublishRequest.fromJson(requestJson(scope: 'payslip', totals: false));
      expect(r.snapshot.totals, isNull);
      expect(r.isPayslip, isTrue);
      expect(r.checkId, 'p1');
      expect(r.payslipUser!.fullName, 'Ravi Kumar');
      expect(publishRequestScopeLabel(r), contains('Ravi Kumar'));
    });

    test('publish check parses reasons and makers', () {
      final c = PublishCheck.fromJson(checkJson(me: ['EDITED_PAYSLIP'], open: {'id': 'x', 'status': 'submitted'}));
      expect(c.makerChecker, isTrue);
      expect(c.callerIsMaker, isTrue);
      expect(c.hasOpenRequest, isTrue);
      expect(c.publishablePayslips, 46);
      expect(c.makers.single.reasons, ['PROCESSED_RUN']);
      expect(publishReasonLabel('EDITED_PAYSLIP'), 'edited a payslip in it');
    });

    test('run and payslip carry the newest publish_request', () {
      final run = PayrollRun.fromJson({
        ...runJson(status: 'processed'),
        'publish_request': {
          'id': 'pr1',
          'status': 'submitted',
          'requested_by': userJson('maker', 'Meena'),
          'requested_at': '2026-10-30T10:00:00Z',
          'stale_reason': null,
        },
      });
      expect(run.publishRequest!.isOpen, isTrue);
      expect(publishRefLine(run.publishRequest!), contains('Meena'));
      expect(PayrollRun.fromJson(runJson()).publishRequest, isNull);
      final slip = Payslip.fromJson({
        ...payslipJson(admin: true, status: 'processed'),
        'publish_request': {'id': 'pr2', 'status': 'stale', 'stale_reason': 'PAYSLIP_CHANGED'},
      });
      expect(slip.publishRequest!.status, 'stale');
      expect(publishRefLine(slip.publishRequest!), contains('payslip changed'));
    });

    test('the requester, a maker and the employee cannot decide (cosmetic)', () {
      final r = PublishRequest.fromJson(requestJson());
      expect(canDecidePublishRequest(r, 'checker'), isTrue);
      expect(canDecidePublishRequest(r, 'maker'), isFalse);
      expect(canDecidePublishRequest(r, null), isFalse);
      expect(cannotDecidePublishReason(r, 'maker'), contains('asked for this publish'));
      final slip = PublishRequest.fromJson(requestJson(scope: 'payslip'));
      expect(canDecidePublishRequest(slip, 'emp'), isFalse);
      expect(cannotDecidePublishReason(slip, 'emp'), contains('own payslip'));
      final done = PublishRequest.fromJson(requestJson(status: 'approved'));
      expect(canDecidePublishRequest(done, 'checker'), isFalse);
      // known makers of a decided-looking request
      final withMakers = PublishRequest.fromJson(requestJson(makers: ['checker']));
      expect(canDecidePublishRequest(withMakers, 'checker'), isFalse);
    });

    test('the publish check can take the right away (maker reason or cannot approve)', () {
      final r = PublishRequest.fromJson(requestJson());
      final maker = PublishCheck.fromJson(checkJson(canApprove: true, me: ['EDITED_PAYSLIP']));
      expect(canDecidePublishRequest(r, 'checker', check: maker), isFalse);
      expect(cannotDecidePublishReason(r, 'checker', check: maker), contains('edited a payslip'));
      final no = PublishCheck.fromJson(checkJson());
      expect(canDecidePublishRequest(r, 'checker', check: no), isFalse);
      final yes = PublishCheck.fromJson(checkJson(canApprove: true));
      expect(canDecidePublishRequest(r, 'checker', check: yes), isTrue);
    });

    test('approve result carries the run or the payslip', () {
      final a = PublishApproveResult.fromJson({
        'request': requestJson(status: 'approved'),
        'run': runJson(status: 'published'),
      });
      expect(a.run!.isPublished, isTrue);
      expect(a.payslip, isNull);
    });
  });

  group('delivery', () {
    test('parses counts and items; reminder rules mirror the server', () {
      final d = DeliveryPage.fromJson({
        'counts': {'published': 46, 'pushed': 44, 'viewed': 30, 'downloaded': 12, 'no_device': 2},
        'items': [
          {
            'user': userJson('u1'),
            'payslip_id': 'p1',
            'revision': 1,
            'pushed_at': '2026-10-31T09:00:00Z',
            'push_count': 1,
            'reminders': 0,
            'first_viewed_at': null,
            'first_downloaded_at': null,
            'has_device': true,
          },
          {
            'user': userJson('u2'),
            'payslip_id': 'p2',
            'reminders': 3,
            'first_viewed_at': null,
            'has_device': true,
          },
          {'user': userJson('u3'), 'payslip_id': 'p3', 'has_device': false},
          {'user': userJson('u4'), 'payslip_id': 'p4', 'first_viewed_at': '2026-10-31T10:00:00Z', 'has_device': true},
        ],
        'page': 1,
        'page_size': 50,
        'total': 46,
      });
      expect(d.counts.noDevice, 2);
      expect(d.items.total, 46);
      expect([for (final i in d.items.items) i.canRemind], [true, false, false, false]);
    });

    test('remind summary groups the skip reasons', () {
      final r = RemindResult.fromJson({
        'sent': 14,
        'skipped': [
          {'user_id': 'a', 'reason': 'VIEWED'},
          {'user_id': 'b', 'reason': 'VIEWED'},
          {'user_id': 'c', 'reason': 'TOO_SOON'},
        ],
      });
      expect(r.summary(), '14 reminders sent. 3 skipped: 2 already opened it, 1 reminded in the last 24 hours.');
      expect(const RemindResult(sent: 1).summary(), '1 reminder sent.');
    });
  });

  group('bank accounts', () {
    test('parses a masked account and builds the create body with the password last', () {
      final a = BankAccount.fromJson({
        'id': 'ba1',
        'label': 'HDFC salary account',
        'bank_name': 'HDFC Bank',
        'account': 'XXXXXX4321',
        'ifsc': 'HDFC0001234',
        'is_default': true,
        'active': true,
        'created_at': '2026-10-01T09:00:00Z',
        'created_by': userJson('maker'),
      });
      expect(a.account, 'XXXXXX4321');
      expect(a.isDefault, isTrue);
      final body = const BankAccountInput(
        label: ' Main ',
        bankName: 'HDFC Bank',
        account: '123456789012',
        ifsc: 'hdfc0001234',
        isDefault: true,
      ).toJson('pw');
      expect(body['label'], 'Main');
      expect(body['ifsc'], 'HDFC0001234');
      expect(body['current_password'], 'pw');
      expect(body['is_default'], true);
    });

    test('validators follow the contract patterns', () {
      expect(validateDebitAccount('12345678'), isNotNull);
      expect(validateDebitAccount('123456789'), isNull);
      expect(validateDebitAccount('1234567890123456789'), isNotNull);
      expect(validateDebitAccount('12345678a'), isNotNull);
      expect(validateDebitIfsc('HDFC0001234'), isNull);
      expect(validateDebitIfsc('hdfc0001234'), isNull);
      expect(validateDebitIfsc('HDFC1001234'), isNotNull);
      expect(validateDebitIfsc('HDF0001234'), isNotNull);
      expect(validateBankLabel(''), isNotNull);
      expect(validateBankLabel('x' * 41), isNotNull);
      expect(validateBankLabel('Main'), isNull);
    });
  });

  group('payment batches', () {
    test('parses lines: snapshot versus current account, flags, paid reference (masked)', () {
      final b = PaymentBatch.fromJson(batchJson(
        lines: [
          lineJson(changed: true, flags: ['RECENT_BANK_CHANGE']),
          lineJson(id: 'l2', seq: 2, status: 'paid', source: 'current'),
        ],
        blockers: [
          {'user': userJson('u9'), 'payslip_id': 'p9', 'reason': 'NO_BANK_DETAILS'},
        ],
      ),);
      final l = b.lines.first;
      expect(l.bankChanged, isTrue);
      expect(l.account, 'XXXXXX1234');
      expect(l.currentAccount, 'XXXXXX9876');
      expect(l.currentIfsc, 'ICIC0000456');
      expect(l.usesCurrent, isFalse);
      expect(l.flags, ['BANK_DETAILS_CHANGED', 'RECENT_BANK_CHANGE']);
      expect(b.lines[1].reference, 'XXXX7788');
      expect(b.lines[1].paidOn, '2026-10-31');
      expect(b.lines[1].currentAccount, isNull);
      expect(b.blockers.single.reason, 'NO_BANK_DETAILS');
      expect(blockerLabel('NO_BANK_DETAILS'), 'No bank details');
      for (final r in ['UNDERPAID_CHAIN', 'FAILED_BY_YOU']) {
        expect(blockerLabel(r), isNot(r), reason: r); // plain language, not the code
      }
      expect(blockerLabel('UNDERPAID_CHAIN'), contains('less than their net pay'));
      expect(lineFlagLabel('PREVIOUSLY_FAILED'), contains('failed'));
      expect(lineFlagLabel('PREVIOUSLY_FAILED'), isNot('PREVIOUSLY_FAILED'));
      expect(phase5AlertText['PAYMENT_UNDERPAID'], contains('less than their net pay'));
      expect(b.total, '70600.00');
      expect(b.debitAccount!.account, 'XXXXXX4321');
      expect(b.canCancel, isTrue);
      expect(b.hasFile, isFalse);
    });

    test('status helpers', () {
      expect(PaymentBatch.fromJson(batchJson(status: 'approved')).hasFile, isTrue);
      expect(PaymentBatch.fromJson(batchJson(status: 'exported')).canCancel, isFalse);
      expect(PaymentBatch.fromJson(batchJson(status: 'exported')).fileSha256, hasLength(64));
      expect(PaymentBatch.fromJson(batchJson(status: 'cancelled')).hasFile, isFalse);
    });

    test('the maker, the submitter and a paid employee cannot approve (cosmetic)', () {
      final b = PaymentBatch.fromJson(batchJson(status: 'submitted', submitter: 'sub'));
      expect(canDecideBatch(b, 'checker'), isTrue);
      expect(canDecideBatch(b, 'maker'), isFalse);
      expect(canDecideBatch(b, 'sub'), isFalse);
      expect(canDecideBatch(b, 'u1'), isFalse);
      expect(cannotDecideBatchReason(b, 'u1'), contains('pays you'));
      expect(cannotDecideBatchReason(b, 'maker'), contains('made or submitted'));
      expect(canDecideBatch(PaymentBatch.fromJson(batchJson()), 'checker'), isFalse);
    });

    test('whoever sent a batch back does not edit it again', () {
      final b = PaymentBatch.fromJson(batchJson(decider: 'checker'));
      expect(canEditBatch(b, 'checker'), isFalse);
      expect(canEditBatch(b, 'maker'), isTrue);
      expect(canEditBatch(PaymentBatch.fromJson(batchJson(status: 'submitted')), 'maker'), isFalse);
    });

    test('edit body: refresh sends only the version; sources send "current"', () {
      expect(const PaymentBatchEdit().toJson(3), {'expected_version': 3});
      final body = const PaymentBatchEdit(
        valueDate: '2026-11-05',
        excludeLineIds: ['a'],
        includeLineIds: ['b'],
        currentAccountLineIds: ['c'],
      ).toJson(2);
      expect(body, {
        'expected_version': 2,
        'value_date': '2026-11-05',
        'exclude_line_ids': ['a'],
        'include_line_ids': ['b'],
        'account_sources': [
          {'line_id': 'c', 'source': 'current'},
        ],
      });
      expect(
        const PaymentBatchCreate(valueDate: '2026-10-31').toJson(),
        {'debit_account_id': null, 'value_date': '2026-10-31', 'user_ids': null},
      );
    });

    test('confirm body', () {
      expect(
        const PaymentConfirm(paidOn: '2026-10-31', allPaid: true, reference: ' UTR1 ').toJson('pw'),
        {'paid_on': '2026-10-31', 'lines': null, 'all_paid': true, 'reference': 'UTR1', 'current_password': 'pw'},
      );
      final per = const PaymentConfirm(paidOn: '2026-10-31', lines: [
        ConfirmLine(lineId: 'a', status: 'paid', reference: 'U1'),
        ConfirmLine(lineId: 'b', status: 'failed'),
      ],).toJson('pw');
      expect(per['all_paid'], false);
      expect(per['reference'], isNull);
      expect(per['lines'], [
        {'line_id': 'a', 'status': 'paid', 'reference': 'U1'},
        {'line_id': 'b', 'status': 'failed'},
      ]);
    });

    test('date rules', () {
      final today = DateTime(2026, 10, 25, 14);
      expect(validateValueDate(DateTime(2026, 10, 25), today), isNull);
      expect(validateValueDate(DateTime(2026, 11, 24), today), isNull);
      expect(validateValueDate(DateTime(2026, 11, 25), today), isNotNull);
      expect(validateValueDate(DateTime(2026, 10, 24), today), isNotNull);
      expect(validateValueDate(null, today), isNotNull);
      final exported = DateTime(2026, 10, 23, 18);
      expect(validatePaidOn(DateTime(2026, 10, 25), today, exportedAt: exported), isNull);
      expect(validatePaidOn(DateTime(2026, 10, 23), today, exportedAt: exported), isNull);
      expect(validatePaidOn(DateTime(2026, 10, 22), today, exportedAt: exported), isNotNull);
      expect(validatePaidOn(DateTime(2026, 10, 26), today, exportedAt: exported), isNotNull);
    });

    test('bank reference rules', () {
      expect(validateBankReference(''), isNull);
      expect(validateBankReference('UTR 123/ab-1.x_'), isNull);
      expect(validateBankReference('bad;ref'), isNotNull);
      expect(validateBankReference('x' * 41), isNotNull);
    });
  });

  group('error messages', () {
    String msg(String code, {List<FieldError> details = const []}) =>
        phase5ErrorMessage(ApiException(code: code, message: '', details: details));

    test('every 5a code has plain language', () {
      for (final c in [
        'PUBLISH_REQUEST_REQUIRED',
        'PUBLISH_REQUEST_EXISTS',
        'MAKER_CHECKER_OFF',
        'PUBLISH_STALE',
        'NOT_SUBMITTED',
        'NOT_DRAFT',
        'NOT_APPROVED',
        'BATCH_STALE',
        'NOTHING_TO_PAY',
        'IN_USE',
        'TOO_MANY_ROWS',
        'INVALID_DATE',
        'SECOND_PERSON_REQUIRED',
        'FORBIDDEN',
        'COMPONENTS_CHANGED',
        'CONFLICT',
        'BATCH_CLOSED',
      ]) {
        final m = msg(c);
        expect(m, isNotEmpty);
        expect(m, isNot(contains(c)), reason: c);
        expect(m.toLowerCase(), isNot(contains('maker-checker')));
      }
      expect(msg('PUBLISH_REQUEST_REQUIRED'), contains('publish request'));
      expect(msg('REAUTH_FAILED'), 'Your password was not correct.');
    });

    test('BATCH_STALE lists the lines by number', () {
      final m = msg('BATCH_STALE', details: const [
        FieldError('lines[3]', 'BANK_DETAILS_CHANGED'),
        FieldError('lines[7]', 'AMOUNT_CHANGED'),
      ],);
      expect(m, contains('Line 3: the bank details changed'));
      expect(m, contains('Line 7: the amount changed'));
      expect(m, contains('Refresh lines'));
    });

    test('NOTHING_TO_PAY groups the blockers (field is an employee id, never shown)', () {
      final m = msg('NOTHING_TO_PAY', details: const [
        FieldError('emp-1', 'NO_BANK_DETAILS'),
        FieldError('emp-2', 'NO_BANK_DETAILS'),
        FieldError('emp-3', 'HELD'),
      ],);
      expect(m, contains('2 x No bank details'));
      expect(m, contains('1 x Payslip is on hold'));
      expect(m, isNot(contains('emp-1')));
    });

    test('unknown codes fall through to the shared messages', () {
      expect(msg('VERSION_CONFLICT'), contains('Reload'));
      expect(msg('PAYROLL_LOCKED'), contains('locked'));
    });

    test('stale codes reload the screen', () {
      expect(isStalePhase5Error(const ApiException(code: 'BATCH_STALE', message: '')), isTrue);
      expect(isStalePhase5Error(const ApiException(code: 'REAUTH_FAILED', message: '')), isFalse);
    });
  });

  group('details and settings', () {
    test('details carry bank_changed_at', () {
      final d = PayrollDetails.fromJson({
        'user_id': 'u1',
        'payment_mode': 'bank_transfer',
        'include_in_payroll': true,
        'bank_changed_at': '2026-10-20T08:00:00Z',
      });
      expect(d.bankChangedAt, isNotNull);
      expect(
        PayrollDetails.fromJson({'user_id': 'u1', 'bank_changed_at': null}).bankChangedAt,
        isNull,
      );
    });

    test('new settings are known and ranged', () {
      final byKey = {for (final s in payrollSettingSpecs) s.key: s};
      expect(byKey['payroll.publish_request_ttl_hours']!.min, 1);
      expect(byKey['payroll.publish_request_ttl_hours']!.max, 336);
      expect(byKey['payroll.bank_change_alert_days']!.min, 7); // a short window would switch the control off
      expect(byKey['payroll.bank_change_alert_days']!.max, 90);
      expect(validatePayrollNumber('6', byKey['payroll.bank_change_alert_days']!), 'At least 7');
      expect(validatePayrollNumber('7', byKey['payroll.bank_change_alert_days']!), isNull);
      expect(byKey['payroll.bank_payment_type']!.options.map((o) => o.$1), ['NEFT', 'RTGS', 'IMPS']);
      expect(byKey['payroll.bank_file_format']!.options.single.$1, 'generic_neft_csv');
      // the second approver is unconditional: the switch is gone
      expect(byKey.containsKey('payroll.bank_file_requires_second_person'), isFalse);
      expect(bankChangeAlertDaysKey, 'payroll.bank_change_alert_days');
    });

    test('nothing sensitive reaches toString of models', () {
      final b = PaymentBatch.fromJson(batchJson());
      expect(b.toString(), startsWith("Instance of '"));
      expect(b.lines.first.toString(), startsWith("Instance of '"));
      const e = ApiException(code: 'BATCH_STALE', message: 'x');
      expect(e.toString(), isNot(contains('XXXXXX')));
    });
  });
}
