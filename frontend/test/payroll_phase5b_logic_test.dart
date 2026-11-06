import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/payroll/domain/phase5b_models.dart';
import 'package:flutter_test/flutter_test.dart';

/// Parsing and pure rules of work package 5b (api-contract.md 19.6 to 19.8, notes in 19.15).

Map<String, dynamic> userJson(String id, [String name = 'Asha Rao']) =>
    {'id': id, 'employee_id': 'EMP-$id', 'full_name': name};

Map<String, dynamic> windowsJson() => {
      'declaration_opens_on': '2026-04-01',
      'declaration_closes_on': '2026-06-30',
      'proof_opens_on': '2027-01-01',
      'proof_closes_on': '2027-02-28',
    };

Map<String, dynamic> sampleRules() => {
      'default_regime': 'new',
      'regimes': {
        'new': {
          'slabs': [
            {'from': '0.00', 'to': '400000.00', 'rate_pct': '0'},
            {'from': '400000.00', 'to': '800000.00', 'rate_pct': '5'},
            {'from': '800000.00', 'to': null, 'rate_pct': '10'},
          ],
          'standard_deduction': '75000.00',
          'rebate': {'income_limit': '1200000.00', 'max': '60000.00', 'marginal_relief': true},
          'surcharge': [
            {'above': '5000000.00', 'rate_pct': '10'},
            {'above': '10000000.00', 'rate_pct': '15'},
          ],
          'surcharge_marginal_relief': true,
          'pt_deductible': false,
          'hra_exemption': false,
          'allowed_sections': <String>[],
        },
        'old': {
          'slabs': [
            {'from': '0.00', 'to': '250000.00', 'rate_pct': '0'},
            {'from': '250000.00', 'to': null, 'rate_pct': '30'},
          ],
          'standard_deduction': '50000.00',
          'rebate': {'income_limit': '500000.00', 'max': '12500.00', 'marginal_relief': false},
          'surcharge': <Object>[],
          'surcharge_marginal_relief': true,
          'pt_deductible': true,
          'hra_exemption': true,
          'allowed_sections': ['80C', 'HRA'],
        },
      },
      'cess_pct': '4',
      'sections': [
        {
          'code': '80C',
          'name': 'Section 80C',
          'limit': null,
          'group': '80C_GROUP',
          'proof_required': true,
          'fields': ['description'],
        },
        {
          'code': 'HRA',
          'name': 'House rent',
          'limit': null,
          'group': null,
          'proof_required': true,
          'fields': ['monthly_rent', 'from_month', 'to_month', 'landlord_name', 'landlord_pan', 'city', 'metro'],
          'landlord_pan_above': '100000.00',
        },
      ],
      'group_limits': [
        {'group': '80C_GROUP', 'limit': '150000.00'},
      ],
      'hra': {'metro_pct': '50', 'non_metro_pct': '40', 'rent_minus_basic_pct': '10'},
    };

Map<String, dynamic> rulesJson({
  String id = 'ru1',
  String fy = '2026-27',
  int version = 2,
  String status = 'draft',
  String? note = 'Reviewed against the Act',
  String creator = 'maker',
  String? submitter,
  Map<String, dynamic>? rules,
  int versionRow = 3,
}) =>
    {
      'id': id,
      'fy': fy,
      'version': version,
      'status': status,
      'rules_sha256': 'a' * 64,
      'note': note,
      'rules': rules ?? sampleRules(),
      'attestation': null,
      'created_by': userJson(creator, 'Meena'),
      'last_edited_by': userJson(creator, 'Meena'),
      'submitted_by': submitter == null ? null : userJson(submitter, 'Ravi'),
      'submitted_at': submitter == null ? null : '2026-10-30T10:00:00Z',
      'verified_by': null,
      'verified_at': null,
      'version_row': versionRow,
      'windows': windowsJson(),
    };

Map<String, dynamic> yearJson({
  String fy = '2026-27',
  int? verified,
  Map<String, dynamic>? open,
  bool current = true,
}) =>
    {'fy': fy, 'verified_version': verified, 'open_version': open, 'windows': windowsJson(), 'current': current};

Map<String, dynamic> declJson({
  String? id = 'd1',
  int version = 5,
  bool canEdit = true,
  String reason = 'DECLARATION_WINDOW',
  String? regime = 'old',
  List<Map<String, dynamic>>? items,
  bool withSubmitted = true,
  bool dirty = false,
  String? user,
  String? enteredBy,
}) {
  final its = items ??
      [
        {
          'id': 'i-1',
          'section': '80C',
          'amount': '150000.00',
          'details': {'description': 'PPF'},
          'flags': <String>[],
        },
      ];
  return {
    'id': id,
    'fy': '2026-27',
    'rules_version': 2,
    'windows': windowsJson(),
    'can_edit': canEdit,
    'edit_reason': reason,
    'draft': {'regime': regime, 'items': its},
    'submitted': withSubmitted ? {'regime': regime, 'items': its, 'version': 3, 'submitted_at': '2026-05-01T10:00:00Z'} : null,
    'dirty': dirty,
    'proofs': {'submitted': 1, 'verified': 2, 'rejected': 0},
    'version': version,
    if (user != null) 'user': userJson(user, 'Asha Rao'),
    if (user != null) 'entered_by': enteredBy == null ? null : userJson(enteredBy, 'Ravi'),
    if (user != null) 'unlocked_until': null,
  };
}

Map<String, dynamic> proofJson({
  String id = 'pf1',
  String status = 'submitted',
  String user = 'emp',
  String claimed = '120000.00',
  int version = 1,
  String itemId = 'i-1',
  String? comment,
}) =>
    {
      'id': id,
      'fy': '2026-27',
      'item_id': itemId,
      'section': '80C',
      'claimed': claimed,
      'files': [
        {'id': 'f1', 'url': '/api/files/file-0001', 'content_type': 'application/pdf'},
      ],
      'status': status,
      'verified': status == 'verified' ? claimed : null,
      'comment': comment,
      'verified_at': null,
      'version': version,
      'user': userJson(user, 'Asha Rao'),
    };

Map<String, dynamic> form16Json({
  String id = 'f16a',
  String user = 'emp',
  String status = 'uploaded',
  String uploader = 'maker',
  String kind = 'form16',
}) =>
    {
      'id': id,
      'user': userJson(user, 'Asha Rao'),
      'fy': '2026-27',
      'kind': kind,
      'status': status,
      'sha256': 'b' * 64,
      'uploaded_by': userJson(uploader, 'Meena'),
      'uploaded_at': '2026-05-20T09:00:00Z',
      'published_by': null,
      'published_at': null,
      'withdrawn_at': null,
    };

Map<String, dynamic> dryRunJson({List<Map<String, dynamic>>? matched, List<Map<String, dynamic>>? errors, int files = 2}) => {
      'fy': '2026-27',
      'kind': 'form16',
      'manifest_sha256': 'c' * 64,
      'files': files,
      'matched': matched ??
          [
            {'index': 0, 'user': userJson('u1', 'Asha Rao'), 'sha256': 'd' * 64, 'replaces_id': null},
            {
              'index': 1,
              'user': userJson('u2', 'Ravi Kumar'),
              'sha256': 'e' * 64,
              'replaces_id': 'old1',
              'warnings': ['REPLACES'],
            },
          ],
      'errors': errors ?? <Object>[],
    };

void main() {
  group('financial years and windows', () {
    test('fy validity, bounds and year of a date', () {
      expect(isValidFy('2026-27'), isTrue);
      expect(isValidFy('2026-28'), isFalse);
      expect(isValidFy('2026'), isFalse);
      expect(isValidFy('2099-00'), isTrue);
      expect(fyBounds('2026-27'), (DateTime(2026, 4, 1), DateTime(2027, 3, 31)));
      expect(fyOf(DateTime(2026, 3, 31)), '2025-26');
      expect(fyOf(DateTime(2026, 4, 1)), '2026-27');
      expect(fyOf(DateTime(2026, 12, 31)), '2026-27');
    });

    test('windows: open today, both dates or neither, inside the year', () {
      final w = TaxWindows.fromJson(windowsJson());
      expect(w.declarationOpen(DateTime(2026, 6, 30)), isTrue);
      expect(w.declarationOpen(DateTime(2026, 7, 1)), isFalse);
      expect(w.proofOpen(DateTime(2027, 1, 1)), isTrue);
      expect(validateWindows(w, '2026-27'), isNull);
      expect(validateWindows(const TaxWindows(declarationOpensOn: '2026-04-01'), '2026-27'), contains('both dates'));
      expect(
        validateWindows(const TaxWindows(proofOpensOn: '2027-03-01', proofClosesOn: '2027-02-01'), '2026-27'),
        contains('cannot close before'),
      );
      expect(
        validateWindows(const TaxWindows(proofOpensOn: '2027-01-01', proofClosesOn: '2027-04-01'), '2026-27'),
        contains('inside'),
      );
      expect(validateWindows(const TaxWindows(), '2026-27'), isNull); // both closed is allowed
      expect(const TaxWindows().declarationText, 'Closed');
    });
  });

  group('rules', () {
    test('parse: versions, people, windows, sections', () {
      final r = TaxRules.fromJson(rulesJson(status: 'submitted', submitter: 'sub'));
      expect(r.fy, '2026-27');
      expect(r.isSubmitted, isTrue);
      expect(r.versionRow, 3);
      expect(r.sections.map((s) => s.code), ['80C', 'HRA']);
      expect(r.sections.last.isHra, isTrue);
      expect(r.windows.declarationClosesOn, '2026-06-30');
      expect(r.noteIsUnverified, isFalse);
      expect(TaxRules.fromJson(rulesJson(note: 'UNVERIFIED seed copied from FY 2025-26')).noteIsUnverified, isTrue);
    });

    test('maker-checker: creator, editor and submitter cannot verify', () {
      final r = TaxRules.fromJson(rulesJson(status: 'submitted', creator: 'maker', submitter: 'sub'));
      expect(canDecideRules(r, 'maker'), isFalse);
      expect(canDecideRules(r, 'sub'), isFalse);
      expect(canDecideRules(r, 'checker'), isTrue);
      expect(cannotDecideRulesReason(r, 'maker'), contains('different person'));
      expect(cannotDecideRulesReason(r, 'checker'), isNull);
      final draft = TaxRules.fromJson(rulesJson());
      expect(canDecideRules(draft, 'checker'), isFalse);
    });

    test('slabs must be contiguous from zero, ascending, with one open last slab', () {
      List<Map<String, Object?>> slabs(List<List<Object?>> rows) => [
            for (final r in rows) {'from': r[0], 'to': r[1], 'rate_pct': r[2]},
          ];
      expect(validateSlabs('s', slabs([['0.00', '100.00', '0'], ['100.00', null, '5']])), isEmpty);
      expect(validateSlabs('s', slabs([['10.00', null, '0']])).single.path, 's[0].from');
      expect(validateSlabs('s', slabs([['0.00', '100.00', '0'], ['150.00', null, '5']])).single.path, 's[1].from');
      expect(validateSlabs('s', slabs([['0.00', '100.00', '0']])).single.path, 's[0].to'); // last slab closed
      expect(validateSlabs('s', slabs([['0.00', null, '0'], ['0.00', null, '5']])).first.path, 's[0].to');
      expect(validateSlabs('s', slabs([['0.00', '100.00', '51'], ['100.00', null, '5']])).single.path, 's[0].rate_pct');
      expect(validateSlabs('s', slabs([['0.00', '100.00', '5.123'], ['100.00', null, '5']])).single.path, 's[0].rate_pct');
      expect(validateSlabs('s', slabs([['0.00', '0.00', '0'], ['0.00', null, '5']])).single.path, 's[0].to');
      expect(validateSlabs('s', const []).single.message, contains('at least one'));
    });

    test('local validation passes the sample and names unknown allowed sections', () {
      expect(validateRulesLocally(sampleRules()), isEmpty);
      final bad = sampleRules();
      ((bad['regimes'] as Map)['old'] as Map)['allowed_sections'] = ['80C', '80CCC'];
      final issues = validateRulesLocally(bad);
      expect(issues.single.path, 'rules.regimes.old.allowed_sections[1]');
      final dup = sampleRules();
      ((dup['sections'] as List)[1] as Map)['code'] = '80C';
      expect(validateRulesLocally(dup).map((i) => i.path), contains('rules.sections[1].code'));
      final badCode = sampleRules();
      ((badCode['sections'] as List)[0] as Map)['code'] = 'x';
      expect(validateRulesLocally(badCode).map((i) => i.path), contains('rules.sections[0].code'));
      final badMoney = sampleRules();
      ((badMoney['regimes'] as Map)['new'] as Map)['standard_deduction'] = '12x';
      expect(validateRulesLocally(badMoney).map((i) => i.path), contains('rules.regimes.new.standard_deduction'));
    });

    test('money and rate checks', () {
      expect(isRulesMoney('400000.00'), isTrue);
      expect(isRulesMoney('120000000.00'), isTrue);
      expect(isRulesMoney('120000000.01'), isFalse);
      expect(isRulesMoney('1.234'), isFalse);
      expect(isRulesMoney(null), isFalse);
      expect(normalizeRulesMoney('400000'), '400000.00');
      expect(normalizeRulesMoney('12.5'), '12.50');
      expect(normalizeRulesMoney('abc'), isNull);
      expect(isRulesRate('50'), isTrue);
      expect(isRulesRate('50.01'), isFalse);
      expect(isRulesRate('12.34'), isTrue);
    });

    test('diff lists changed, added and removed paths', () {
      final a = sampleRules();
      final b = sampleRules();
      ((b['regimes'] as Map)['new'] as Map)['standard_deduction'] = '80000.00';
      (b['hra'] as Map)['extra_pct'] = '5';
      b['cess_pct'] = null;
      final changes = diffRules(a, b);
      expect(changes.map((c) => c.path), containsAll(['regimes.new.standard_deduction', 'hra.extra_pct', 'cess_pct']));
      final std = changes.firstWhere((c) => c.path == 'regimes.new.standard_deduction');
      expect((std.before, std.after), ('75000.00', '80000.00'));
      expect(changes.firstWhere((c) => c.path == 'hra.extra_pct').before, isNull);
      expect(diffRules(a, sampleRules()), isEmpty);
    });

    test('attestation is 20 to 500 characters', () {
      expect(validateAttestation('short'), isNotNull);
      expect(validateAttestation('I verified these rules against the Act on 1 May 2026'), isNull);
      expect(validateAttestation('x' * 501), isNotNull);
    });
  });

  group('declaration items', () {
    test('declaration parse: draft, submitted, flags, proofs, admin extras', () {
      final d = TaxDeclaration.fromJson(declJson(user: 'emp', enteredBy: 'hr', dirty: true));
      expect(d.version, 5);
      expect(d.canEdit, isTrue);
      expect(d.draft.items.single.details['description'], 'PPF');
      expect(d.submitted!.version, 3);
      expect(d.proofs.verified, 2);
      expect(d.enteredBy!.id, 'hr');
      expect(d.dirty, isTrue);
      final none = TaxDeclaration.fromJson(declJson(id: null, version: 0, withSubmitted: false, items: [], regime: null));
      expect(none.id, isNull);
      expect(none.isSubmitted, isFalse);
      expect(none.draft.items, isEmpty);
    });

    test('edit reasons control regime and proofs', () {
      expect(TaxDeclaration.fromJson(declJson()).regimeLocked, isFalse);
      expect(TaxDeclaration.fromJson(declJson(reason: 'PROOF_WINDOW')).regimeLocked, isTrue);
      expect(TaxDeclaration.fromJson(declJson(reason: 'PROOF_WINDOW')).canAddProofs, isTrue);
      expect(TaxDeclaration.fromJson(declJson(reason: 'UNLOCKED')).canAddProofs, isTrue);
      expect(TaxDeclaration.fromJson(declJson()).canAddProofs, isFalse);
      expect(TaxDeclaration.fromJson(declJson(canEdit: false, reason: 'CLOSED')).canAddProofs, isFalse);
    });

    test('PAN: format, masked values refused', () {
      expect(validatePan('ABCDE1234F'), isNull);
      expect(validatePan('abcde1234f'), isNull);
      expect(validatePan(''), isNull);
      expect(validatePan('', required: true), isNotNull);
      expect(validatePan('XXXXX1234X'), contains('hidden'));
      expect(validatePan('ABC'), isNotNull);
      expect(isMaskedPan('xxxxx1234x'), isTrue);
    });

    test('annual amount: 0.01 to 12 crore, two decimals', () {
      expect(validateAnnualAmount('150000'), isNull);
      expect(validateAnnualAmount('150000.50'), isNull);
      expect(validateAnnualAmount('0'), isNotNull);
      expect(validateAnnualAmount('0', allowZero: true), isNull);
      expect(validateAnnualAmount('120000000.01'), isNotNull);
      expect(validateAnnualAmount('1.234'), isNotNull);
      expect(validateAnnualAmount(''), isNotNull);
      expect(validateMonthlyRent('10000000'), isNull);
      expect(validateMonthlyRent('10000000.01'), isNotNull);
    });

    test('months and the yearly rent', () {
      expect(validateMonthInFy('2026-04', '2026-27'), isNull);
      expect(validateMonthInFy('2027-03', '2026-27'), isNull);
      expect(validateMonthInFy('2027-04', '2026-27'), contains('inside'));
      expect(validateMonthInFy('2026-13', '2026-27'), isNotNull);
      expect(monthsBetween('2026-04', '2027-03'), 12);
      expect(monthsBetween('2026-05', '2026-04'), isNull);
      expect(hraAnnualRentPaise({'monthly_rent': '20000', 'from_month': '2026-04', 'to_month': '2027-03'}), 24000000);
      expect(hraAnnualRentPaise({'monthly_rent': '20000'}), isNull);
    });

    test('item wire form: only the fields of the section, HRA amount left to the server', () {
      final hra = SectionDef.fromJson(sampleRules()['sections'][1] as Map<String, dynamic>);
      final item = ItemDraft(
        section: 'HRA',
        amount: '999',
        details: {
          'monthly_rent': '20000',
          'from_month': '2026-04',
          'to_month': '2027-03',
          'landlord_pan': 'abcde1234f',
          'landlord_name': ' R K ',
          'city': '',
          'metro': true,
          'description': 'not a field of HRA',
        },
      );
      final j = item.toJson(hra);
      expect(j.containsKey('amount'), isFalse);
      expect(j['id'], isNull);
      final d = j['details']! as Map<String, Object?>;
      expect(d['monthly_rent'], '20000.00');
      expect(d['landlord_pan'], 'ABCDE1234F');
      expect(d['landlord_name'], 'R K');
      expect(d.containsKey('city'), isFalse);
      expect(d.containsKey('description'), isFalse);
      expect(d['metro'], true);
      final plain = ItemDraft(id: 'i-1', section: '80C', amount: '150000', details: {'description': 'PPF'});
      expect(plain.toJson(null), {
        'id': 'i-1',
        'section': '80C',
        'amount': '150000.00',
        'details': {'description': 'PPF'},
      });
    });

    test('item validation: required rent fields and landlord PAN above the threshold', () {
      final hra = SectionDef.fromJson(sampleRules()['sections'][1] as Map<String, dynamic>);
      ItemDraft rent(String monthly, {String? pan}) => ItemDraft(
            section: 'HRA',
            details: {
              'monthly_rent': monthly,
              'from_month': '2026-04',
              'to_month': '2027-03',
              if (pan != null) 'landlord_pan': pan,
            },
          );
      expect(validateItemDraft(rent('5000'), hra, '2026-27'), isNull); // 60,000 a year
      expect(validateItemDraft(rent('20000'), hra, '2026-27'), contains('Landlord PAN')); // 2,40,000 > 1,00,000
      expect(validateItemDraft(rent('20000', pan: 'ABCDE1234F'), hra, '2026-27'), isNull);
      expect(validateItemDraft(rent('20000', pan: 'XXXXX1234X'), hra, '2026-27'), contains('hidden'));
      expect(validateItemDraft(ItemDraft(section: 'HRA', details: {'monthly_rent': '5000'}), hra, '2026-27'), isNotNull);
      final c80 = SectionDef.fromJson(sampleRules()['sections'][0] as Map<String, dynamic>);
      expect(validateItemDraft(ItemDraft(section: '80C', amount: ''), c80, '2026-27'), isNotNull);
      expect(validateItemDraft(ItemDraft(section: '80C', amount: '1000'), c80, '2026-27'), isNull);
    });

    test('item flags have plain text', () {
      expect(itemFlagLabel('NOT_IN_REGIME'), contains('regime'));
      expect(itemFlagLabel('ABOVE_LIMIT'), contains('limit'));
    });
  });

  group('proofs', () {
    test('parse and decide rules', () {
      final p = TaxProof.fromJson(proofJson());
      expect(p.files.single.isPdf, isTrue);
      expect(p.files.single.extension, '.pdf');
      expect(p.user!.id, 'emp');
      expect(canDecideProof(p, 'hr'), isTrue);
      expect(canDecideProof(p, 'emp'), isFalse);
      expect(canDecideProof(TaxProof.fromJson(proofJson(status: 'verified')), 'hr'), isFalse);
    });

    test('verified amount is 0 to claimed; 0 needs a comment', () {
      expect(validateVerifiedAmount('120000', '120000.00'), isNull);
      expect(validateVerifiedAmount('100000.50', '120000.00'), isNull);
      expect(validateVerifiedAmount('120000.01', '120000.00'), contains('more than'));
      expect(validateVerifiedAmount('0', '120000.00'), contains('comment'));
      expect(validateVerifiedAmount('0', '120000.00', comment: 'illegible'), isNull);
      expect(validateVerifiedAmount('', '120000.00'), isNotNull);
    });

    test('claimed amount is positive and not above the declared amount', () {
      expect(validateClaimedAmount('150000', '150000.00'), isNull);
      expect(validateClaimedAmount('150000.01', '150000.00'), contains('declared'));
      expect(validateClaimedAmount('0', '150000.00'), isNotNull);
    });

    test('comment length', () {
      expect(validateComment('', required: true), isNotNull);
      expect(validateComment(''), isNull);
      expect(validateComment('ab'), isNotNull);
      expect(validateComment('x' * 201), isNotNull);
    });
  });

  group('Form 16', () {
    test('dry run: matched, replaces warning, errors block the commit', () {
      final ok = Form16DryRun.fromJson(dryRunJson());
      expect(ok.canCommit, isTrue);
      expect(ok.matched.first.replaces, isFalse);
      expect(ok.matched.last.replaces, isTrue);
      final bad = Form16DryRun.fromJson(dryRunJson(
        matched: [
          {'index': 0, 'user': userJson('u1'), 'sha256': 'd' * 64, 'replaces_id': null},
        ],
        errors: [
          {'index': 1, 'message': 'UNMATCHED'},
        ],
      ),);
      expect(bad.canCommit, isFalse);
      expect(form16ErrorLabel(bad.errors.single.message), contains('No employee'));
      expect(form16ErrorLabel('AMBIGUOUS'), contains('more than one'));
      expect(Form16DryRun.fromJson(dryRunJson(matched: [], files: 0)).canCommit, isFalse);
    });

    test('upload selection limits', () {
      ({String name, int size}) f(String n, int s) => (name: n, size: s);
      expect(validateForm16Selection([f('a.pdf', 1000)]), isNull);
      expect(validateForm16Selection([]), isNotNull);
      expect(validateForm16Selection([f('a.txt', 10)]), contains('PDF'));
      expect(validateForm16Selection([f('a.pdf', form16MaxFileBytes + 1)]), contains('2 MB'));
      expect(validateForm16Selection([for (var i = 0; i < 21; i++) f('a$i.pdf', 10)]), contains('20'));
      expect(
        validateForm16Selection([for (var i = 0; i < 20; i++) f('a$i.pdf', 2 * 1024 * 1024)]),
        isNull, // 40 MB exactly
      );
    });

    test('documents, publish result and who may publish', () {
      final d = Form16Doc.fromJson(form16Json());
      expect(d.isUploaded, isTrue);
      expect(form16KindLabel('part_a'), 'Form 16 Part A');
      expect(canPublishDoc(d, 'checker'), isTrue);
      expect(canPublishDoc(d, 'maker'), isFalse); // uploaded it
      expect(canPublishDoc(d, 'maker', secondPerson: false), isTrue);
      expect(canPublishDoc(d, 'emp'), isFalse); // own document
      expect(canPublishDoc(Form16Doc.fromJson(form16Json(status: 'published')), 'checker'), isFalse);
      final r = Form16PublishResult.fromJson({
        'published': 3,
        'failed': [
          {'id': 'x', 'reason': 'SHA_MISMATCH'},
        ],
      });
      expect(r.published, 3);
      expect(form16PublishFailLabel(r.failed.single.reason), contains('changed'));
      expect(Form16CommitResult.fromJson({'upload_batch_id': 'b', 'created': 3, 'replaced': 1}).replaced, 1);
      expect(MyForm16.parseList({
        'items': [
          {'id': 'm1', 'fy': '2026-27', 'kind': 'form16', 'published_at': '2026-06-01T00:00:00Z'},
        ],
      }).single.kind, 'form16',);
    });
  });

  group('error messages', () {
    String msg(String code, {String message = '', List<FieldError> details = const []}) =>
        phase5bErrorMessage(ApiException(code: code, message: message, details: details));

    test('maker-checker, windows and conflicts are explained', () {
      expect(msg('SECOND_PERSON_REQUIRED'), contains('different person'));
      expect(msg('FORBIDDEN'), contains('your own'));
      expect(msg('WINDOW_CLOSED'), contains('window is closed'));
      expect(msg('REGIME_LOCKED'), contains('regime is fixed'));
      expect(msg('CONFLICT'), contains('cancel the earlier one'));
      expect(msg('TAX_RULES_NOT_VERIFIED'), contains('not verified'));
      expect(msg('DRAFT_EXISTS'), contains('already exists'));
      expect(msg('RULES_CHANGED'), contains('changed after you opened'));
      expect(msg('NOT_PENDING'), contains('already decided'));
      expect(msg('ITEM_NOT_SUBMITTED'), contains('Submit the declaration first'));
      expect(msg('LANDLORD_PAN_REQUIRED'), contains('landlord PAN'));
      expect(msg('IMPORT_NOT_CHECKED'), contains('15 minutes'));
      expect(msg('IMPORT_FILE_CHANGED'), contains('not the ones'));
    });

    test('validation errors show the JSON path; unknown codes fall back', () {
      final m = msg('VALIDATION_ERROR', details: const [FieldError('rules.regimes.new.slabs[2].to', 'must be above from')]);
      expect(m, contains('rules.regimes.new.slabs[2].to'));
      expect(m, startsWith('Rules:'));
      expect(msg('SOMETHING_ELSE', message: 'Plain text'), 'Plain text');
      expect(p5bMsg(Exception('x')), contains('Something went wrong'));
    });

    test('codes after which the record is reloaded', () {
      expect(isStalePhase5bError(const ApiException(code: 'VERSION_CONFLICT', message: '')), isTrue);
      expect(isStalePhase5bError(const ApiException(code: 'RULES_CHANGED', message: '')), isTrue);
      expect(isStalePhase5bError(const ApiException(code: 'VALIDATION_ERROR', message: '')), isFalse);
    });
  });

  test('flattenJson lists every value by path', () {
    final flat = flattenJson({
      'a': {'b': 1, 'c': [2, 3]},
      'd': null,
    });
    expect(flat, {'a.b': '1', 'a.c[0]': '2', 'a.c[1]': '3', 'd': 'none'});
  });
}
