import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/payroll/data/payroll_repository.dart';
import 'package:employee_connect/features/payroll/domain/phase5b_models.dart';
import 'package:flutter_test/flutter_test.dart';

import 'payroll_phase5b_logic_test.dart'
    show declJson, dryRunJson, form16Json, proofJson, rulesJson, sampleRules, userJson, windowsJson, yearJson;

/// Paths, bodies and parsing of the 5b calls (api-contract.md 19.6 to 19.8) against a stubbed Dio transport.
class _Call {
  _Call(this.method, this.path, this.body, this.query);
  final String method;
  final String path;
  final Object? body;
  final Map<String, dynamic> query;
}

class _Adapter implements HttpClientAdapter {
  _Adapter(this.reply);
  final (int, Object?, Map<String, List<String>>) Function(_Call call) reply;
  final List<_Call> calls = [];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    Object? body = options.data;
    if (body is String && body.isNotEmpty) body = jsonDecode(body);
    final c = _Call(options.method, options.path, body, options.queryParameters);
    calls.add(c);
    final (status, json, headers) = reply(c);
    final text = json is String ? json : (json == null ? '' : jsonEncode(json));
    return ResponseBody.fromString(
      text,
      status,
      headers: {
        if (json is! String) Headers.contentTypeHeader: [Headers.jsonContentType],
        ...headers,
      },
    );
  }

  @override
  void close({bool force = false}) {}
}

(PayrollRepository, _Adapter) _repo(Object? Function(_Call) reply, {int status = 200}) {
  final a = _Adapter((c) => (status, reply(c), <String, List<String>>{}));
  final dio = Dio(BaseOptions(baseUrl: 'http://x/api'))..httpClientAdapter = a;
  return (PayrollRepository(dio), a);
}

Map<String, dynamic> page(List<Map<String, dynamic>> items) => {'items': items, 'page': 1, 'page_size': 20, 'total': items.length};

void main() {
  test('tax years and rule versions', () async {
    final (repo, a) = _repo((c) {
      if (c.path.endsWith('/years')) {
        return {
          'items': [
            yearJson(verified: 1, open: {'id': 'ru2', 'version': 2, 'status': 'submitted'}),
          ],
        };
      }
      return {'items': [rulesJson(), rulesJson(id: 'ru1', version: 1, status: 'verified')]};
    });
    final years = await repo.taxYears();
    expect(a.calls.last.path, '/admin/payroll/tax/years');
    expect(years.single.verifiedVersion, 1);
    expect(years.single.openVersion!.status, 'submitted');
    expect(years.single.windows.proofClosesOn, '2027-02-28');
    final versions = await repo.taxRuleVersions('2026-27');
    expect(a.calls.last.path, '/admin/payroll/tax/years/2026-27/rules');
    expect(versions.map((v) => v.version), [2, 1]);
  });

  test('create draft, save (note optional), submit, withdraw, reject', () async {
    final (repo, a) = _repo((c) => rulesJson());
    await repo.createTaxRules('2026-27');
    expect(a.calls.last.method, 'POST');
    expect(a.calls.last.path, '/admin/payroll/tax/years/2026-27/rules');
    expect(a.calls.last.body, <String, Object?>{});
    await repo.taxRules('ru1');
    expect(a.calls.last.path, '/admin/payroll/tax/rules/ru1');
    await repo.saveTaxRules('ru1', versionRow: 3, rules: {'a': 1});
    expect(a.calls.last.method, 'PUT');
    expect(a.calls.last.body, {
      'version_row': 3,
      'rules': {'a': 1},
    });
    await repo.saveTaxRules('ru1', versionRow: 3, rules: {'a': 1}, note: '');
    expect((a.calls.last.body! as Map)['note'], ''); // empty removes the note
    await repo.submitTaxRules('ru1', versionRow: 4);
    expect(a.calls.last.path, '/admin/payroll/tax/rules/ru1/submit');
    expect(a.calls.last.body, {'version_row': 4});
    await repo.withdrawTaxRules('ru1', versionRow: 5);
    expect(a.calls.last.path, '/admin/payroll/tax/rules/ru1/withdraw');
    await repo.rejectTaxRules('ru1', versionRow: 5, comment: ' wrong slab ');
    expect(a.calls.last.path, '/admin/payroll/tax/rules/ru1/reject');
    expect(a.calls.last.body, {'version_row': 5, 'comment': 'wrong slab'});
  });

  test('verify sends the reviewed fingerprint, the attestation and the password', () async {
    final (repo, a) = _repo((c) => rulesJson(status: 'verified'));
    final r = await repo.verifyTaxRules(
      'ru1',
      versionRow: 4,
      rulesSha256: 'a' * 64,
      attestation: ' I verified these rules against the Act on 1 May ',
      currentPassword: 'pw',
    );
    expect(r.isVerified, isTrue);
    expect(a.calls.last.path, '/admin/payroll/tax/rules/ru1/verify');
    expect(a.calls.last.body, {
      'version_row': 4,
      'rules_sha256': 'a' * 64,
      'attestation': 'I verified these rules against the Act on 1 May',
      'current_password': 'pw',
    });
  });

  test('windows are replaced as a whole', () async {
    final (repo, a) = _repo((c) => {'fy': '2026-27', 'windows': windowsJson()});
    final w = await repo.saveTaxWindows(
      '2026-27',
      const TaxWindows(declarationOpensOn: '2026-04-01', declarationClosesOn: '2026-06-30'),
    );
    expect(w.proofClosesOn, '2027-02-28');
    expect(a.calls.last.method, 'PUT');
    expect(a.calls.last.path, '/admin/payroll/tax/years/2026-27/windows');
    expect(a.calls.last.body, {
      'declaration_opens_on': '2026-04-01',
      'declaration_closes_on': '2026-06-30',
      'proof_opens_on': null,
      'proof_closes_on': null,
    });
  });

  test('declarations: list, detail with reveal, on behalf save and submit, unlock', () async {
    final (repo, a) = _repo((c) {
      if (c.path.endsWith('/declarations') && c.method == 'GET') {
        return page([
          {
            'user': userJson('emp'),
            'fy': '2026-27',
            'status': 'submitted',
            'regime': 'old',
            'submitted_at': '2026-05-01T10:00:00Z',
            'dirty': false,
            'items': 7,
            'proofs': {'submitted': 1, 'verified': 2, 'rejected': 0},
            'flags': ['NO_PAN'],
          },
        ]);
      }
      return declJson(user: 'emp', enteredBy: 'hr');
    });
    final p = await repo.taxDeclarations(const TaxDeclarationQuery(fy: '2026-27', status: 'submitted', q: ' asha '));
    expect(p.items.single.flags, ['NO_PAN']);
    expect(p.items.single.proofs.verified, 2);
    expect(a.calls.last.query, {'fy': '2026-27', 'status': 'submitted', 'q': 'asha', 'page': 1, 'page_size': 20});
    await repo.taxDeclarationOf('emp', fy: '2026-27', reveal: true);
    expect(a.calls.last.path, '/admin/payroll/tax/declarations/emp');
    expect(a.calls.last.query, {'fy': '2026-27', 'reveal': true});
    final saved = await repo.saveDeclarationOnBehalf(
      'emp',
      const DeclarationSave(fy: '2026-27', expectedVersion: 5, regime: 'old', items: [
        {'id': null, 'section': '80C', 'amount': '1000.00', 'details': <String, Object?>{}},
      ],),
      currentPassword: 'pw',
    );
    expect(saved.enteredBy!.id, 'hr');
    expect(a.calls.last.method, 'PUT');
    expect((a.calls.last.body! as Map)['current_password'], 'pw');
    expect((a.calls.last.body! as Map)['expected_version'], 5);
    await repo.submitDeclarationOnBehalf('emp', fy: '2026-27', expectedVersion: 6, currentPassword: 'pw');
    expect(a.calls.last.path, '/admin/payroll/tax/declarations/emp/submit');
    expect(a.calls.last.body, {'fy': '2026-27', 'expected_version': 6, 'current_password': 'pw'});
    await repo.unlockDeclaration('emp', fy: '2026-27', until: '2026-07-15', reason: ' late joiner ');
    expect(a.calls.last.path, '/admin/payroll/tax/declarations/emp/unlock');
    expect(a.calls.last.body, {'fy': '2026-27', 'until': '2026-07-15', 'reason': 'late joiner'});
  });

  test('proofs: list, verify (comment optional), reject', () async {
    final (repo, a) = _repo((c) {
      if (c.method == 'GET') return page([proofJson()]);
      return proofJson(status: 'verified');
    });
    final list = await repo.taxProofs(const TaxProofQuery(fy: '2026-27', userId: 'emp'));
    expect(list.items.single.claimed, '120000.00');
    expect(a.calls.last.query, {'fy': '2026-27', 'status': 'submitted', 'user_id': 'emp', 'page': 1, 'page_size': 20});
    await repo.verifyTaxProof('pf1', expectedVersion: 1, verified: '100000.00');
    expect(a.calls.last.path, '/admin/payroll/tax/proofs/pf1/verify');
    expect(a.calls.last.body, {'expected_version': 1, 'verified': '100000.00', 'comment': null});
    await repo.rejectTaxProof('pf1', expectedVersion: 1, comment: ' blurry ');
    expect(a.calls.last.path, '/admin/payroll/tax/proofs/pf1/reject');
    expect(a.calls.last.body, {'expected_version': 1, 'comment': 'blurry'});
  });

  test('a proof file is fetched by its file id only (a tampered link cannot redirect the token)', () async {
    final (repo, a) = _repo((c) => 'pdf-bytes');
    await repo.taxProofFile(const ProofFile(id: 'file-0001', url: '/api/files/file-0001', contentType: 'application/pdf'));
    expect(a.calls.last.path, '/files/file-0001');
    await expectLater(
      repo.taxProofFile(const ProofFile(id: 'x', url: 'https://evil.example/steal', contentType: 'application/pdf')),
      throwsA(isA<ApiException>().having((e) => e.code, 'code', 'NOT_FOUND')),
    );
  });

  test('Form 16 dry run is a multipart without the password; the commit adds manifest and password', () async {
    final (repo, a) = _repo((c) {
      final data = c.body;
      final isDry = c.query['dry_run'] == true;
      expect(data, isA<FormData>());
      return isDry ? dryRunJson() : {'upload_batch_id': 'b1', 'created': 2, 'replaced': 1};
    });
    final files = [
      Form16UploadFile('EMP-u1_form16.pdf', Uint8List.fromList([1, 2, 3])),
      Form16UploadFile('EMP-u2_form16.pdf', Uint8List.fromList([4, 5])),
    ];
    final dry = await repo.form16DryRun('2026-27', 'form16', files);
    expect(dry.canCommit, isTrue);
    expect(a.calls.last.method, 'POST');
    expect(a.calls.last.path, '/admin/payroll/tax/form16/upload');
    expect(a.calls.last.query, {'fy': '2026-27', 'kind': 'form16', 'dry_run': true});
    var form = a.calls.last.body! as FormData;
    expect(form.files.map((f) => f.key), ['files', 'files']);
    expect(form.files.map((f) => f.value.filename), ['EMP-u1_form16.pdf', 'EMP-u2_form16.pdf']);
    expect(form.fields, isEmpty);
    final done = await repo.form16Commit('2026-27', 'form16', files, manifestSha256: dry.manifestSha256, currentPassword: 'pw');
    expect((done.created, done.replaced), (2, 1));
    expect(a.calls.last.query['dry_run'], false);
    form = a.calls.last.body! as FormData;
    expect({for (final f in form.fields) f.key: f.value}, {'manifest_sha256': 'c' * 64, 'current_password': 'pw'});
  });

  test('Form 16 list, admin file, publish with reviewed hashes, withdraw', () async {
    final (repo, a) = _repo((c) {
      if (c.method == 'GET' && c.path.endsWith('/file')) return 'pdf-bytes';
      if (c.method == 'GET') return page([form16Json()]);
      if (c.path.endsWith('/publish')) {
        return {
          'published': 1,
          'failed': [
            {'id': 'f16b', 'reason': 'SECOND_PERSON_REQUIRED'},
          ],
        };
      }
      return <String, Object?>{};
    });
    final p = await repo.form16Documents(const Form16Query(fy: '2026-27', kind: 'form16', status: 'uploaded', q: ' asha '));
    expect(p.items.single.user.id, 'emp');
    expect(a.calls.last.query, {'fy': '2026-27', 'kind': 'form16', 'status': 'uploaded', 'q': 'asha', 'page': 1, 'page_size': 20});
    await repo.form16AdminFile('f16a');
    expect(a.calls.last.path, '/admin/payroll/tax/form16/f16a/file');
    final r = await repo.publishForm16(
      '2026-27',
      [(id: 'f16a', sha256: 'b' * 64), (id: 'f16b', sha256: 'b' * 64)],
      currentPassword: 'pw',
    );
    expect(r.published, 1);
    expect(r.failed.single.reason, 'SECOND_PERSON_REQUIRED');
    expect(a.calls.last.body, {
      'fy': '2026-27',
      'documents': [
        {'id': 'f16a', 'sha256': 'b' * 64},
        {'id': 'f16b', 'sha256': 'b' * 64},
      ],
      'current_password': 'pw',
    });
    await repo.withdrawForm16('f16a', reason: ' wrong person ', currentPassword: 'pw');
    expect(a.calls.last.path, '/admin/payroll/tax/form16/f16a/withdraw');
    expect(a.calls.last.body, {'reason': 'wrong person', 'current_password': 'pw'});
  });

  test('employee: years, declaration, save, submit, proofs, form16', () async {
    final (repo, a) = _repo((c) {
      if (c.path == '/me/tax/years') {
        return {
          'items': [
            {'fy': '2026-27', 'windows': windowsJson(), 'verified': true, 'current': true},
          ],
        };
      }
      if (c.path == '/me/tax/proofs' && c.method == 'GET') return {'items': [proofJson()]};
      if (c.path.startsWith('/me/tax/proofs')) return proofJson();
      if (c.path == '/me/tax/form16') {
        return {
          'items': [
            {'id': 'm1', 'fy': '2026-27', 'kind': 'form16', 'published_at': '2026-06-01T00:00:00Z'},
          ],
        };
      }
      if (c.path.endsWith('/file')) return 'pdf-bytes';
      return declJson();
    });
    expect((await repo.myTaxYears()).single.verified, isTrue);
    await repo.myDeclaration(fy: '2026-27');
    expect(a.calls.last.path, '/me/tax/declaration');
    expect(a.calls.last.query, {'fy': '2026-27'});
    await repo.saveMyDeclaration(const DeclarationSave(fy: '2026-27', expectedVersion: 0, regime: null, items: []));
    expect(a.calls.last.method, 'PUT');
    expect(a.calls.last.body, {'fy': '2026-27', 'expected_version': 0, 'regime': null, 'items': <Object>[]});
    await repo.submitMyDeclaration(fy: '2026-27', expectedVersion: 5);
    expect(a.calls.last.path, '/me/tax/declaration/submit');
    expect(a.calls.last.body, {'fy': '2026-27', 'expected_version': 5});
    await repo.createMyProof(fy: '2026-27', itemId: 'i-1', claimed: '1000.00', fileIds: ['f1']);
    expect(a.calls.last.path, '/me/tax/proofs');
    expect(a.calls.last.body, {'fy': '2026-27', 'item_id': 'i-1', 'claimed': '1000.00', 'file_ids': ['f1']});
    expect((await repo.myProofs(fy: '2026-27')).single.id, 'pf1');
    await repo.cancelMyProof('pf1');
    expect(a.calls.last.path, '/me/tax/proofs/pf1/cancel');
    expect((await repo.myForm16()).single.id, 'm1');
    await repo.myForm16File('m1');
    expect(a.calls.last.path, '/me/tax/form16/m1/file');
  });

  test('server errors keep their code and details', () async {
    final (repo, _) = _repo(
      (c) => {
        'error': {
          'code': 'SECOND_PERSON_REQUIRED',
          'message': 'x',
          'details': <Object>[],
          'request_id': 'q',
        },
      },
      status: 403,
    );
    await expectLater(
      repo.verifyTaxRules('ru1', versionRow: 1, rulesSha256: 'a' * 64, attestation: 'x' * 20, currentPassword: 'pw'),
      throwsA(isA<ApiException>().having((e) => e.code, 'code', 'SECOND_PERSON_REQUIRED')),
    );
    final (repo2, _) = _repo(
      (c) => {
        'error': {
          'code': 'VALIDATION_ERROR',
          'message': 'x',
          'details': [
            {'field': 'rules.regimes.new.slabs[2].to', 'message': 'bad'},
          ],
          'request_id': 'q',
        },
      },
      status: 422,
    );
    await expectLater(
      repo2.saveTaxRules('ru1', versionRow: 1, rules: sampleRules()),
      throwsA(isA<ApiException>().having((e) => e.details.first.field, 'field', 'rules.regimes.new.slabs[2].to')),
    );
  });
}
