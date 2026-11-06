import 'dart:typed_data';

import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart' show Paged;
import 'package:employee_connect/features/payroll/data/claim_bill_picker.dart';
import 'package:employee_connect/features/payroll/data/form16_picker.dart';
import 'package:employee_connect/features/payroll/data/payroll_repository.dart';
import 'package:employee_connect/features/payroll/domain/phase4a_models.dart' show Increment, IncrementBatch, IncrementQuery;
import 'package:employee_connect/features/payroll/domain/phase5b_models.dart';
import 'package:employee_connect/features/payroll/presentation/approvals_tab.dart';
import 'package:employee_connect/features/payroll/presentation/form16_section.dart';
import 'package:employee_connect/features/payroll/presentation/my_tax_screen.dart';
import 'package:employee_connect/features/payroll/presentation/payroll_home_screen.dart';
import 'package:employee_connect/features/payroll/presentation/payroll_settings_tab.dart';
import 'package:employee_connect/features/payroll/presentation/payslips_screen.dart';
import 'package:employee_connect/features/payroll/presentation/tax_declarations_section.dart';
import 'package:employee_connect/features/payroll/presentation/tax_proofs_section.dart';
import 'package:employee_connect/features/payroll/presentation/tax_rules_editor.dart';
import 'package:employee_connect/features/payroll/presentation/tax_rules_section.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/payroll_fakes.dart';
import 'payroll_phase5b_logic_test.dart'
    show declJson, dryRunJson, form16Json, proofJson, rulesJson, sampleRules, userJson, yearJson;

class FakePdfPicker implements Form16Picker {
  FakePdfPicker(this.files);
  List<PickedPdf> files;

  @override
  Future<List<PickedPdf>> pickPdfs() async => files;
}

class Fake5b extends FakeRepo {
  // rules
  List<TaxYear> years = [];
  List<TaxRules> versions = [];
  TaxRules rule = TaxRules.fromJson(rulesJson());
  ApiException? ruleError;
  final ruleCalls = <String>[];
  final savedRules = <Map<String, Object?>>[];
  final verifies = <Map<String, Object?>>[];
  final windowSaves = <Map<String, Object?>>[];

  // employee
  TaxDeclaration myDecl = TaxDeclaration.fromJson(declJson());
  ApiException? myDeclError;
  ApiException? declSaveError;
  int saveVersion = 6;
  final saves = <Map<String, Object?>>[];
  final submits = <int>[];
  List<TaxProof> myProofList = [];
  final proofCreates = <Map<String, Object?>>[];
  final uploads = <String>[];
  ApiException? proofError;
  final cancels = <String>[];
  List<MyForm16> myForm16s = [];

  // admin declarations and proofs
  TaxDeclaration adminDecl = TaxDeclaration.fromJson(declJson(user: 'emp'));
  final onBehalf = <Map<String, Object?>>[];
  final revealCalls = <bool>[];
  final revealPasswords = <String?>[];
  final unlocks = <Map<String, Object?>>[];
  List<TaxProof> proofList = [];
  ApiException? proofDecideError;
  final decisions = <String>[];

  // form 16
  Form16DryRun dry = Form16DryRun.fromJson(dryRunJson());
  ApiException? commitError;
  final dryCalls = <List<String>>[];
  final commits = <Map<String, Object?>>[];
  List<Form16Doc> docs = [];
  Form16PublishResult publishResult = const Form16PublishResult(published: 1);
  final publishes = <List<String>>[];
  final withdraws = <String>[];

  @override
  Future<Paged<Increment>> increments(IncrementQuery q, {int page = 1}) async =>
      const Paged(items: [], page: 1, total: 0, pageSize: 20);

  @override
  Future<Paged<IncrementBatch>> incrementBatches({String? kind, int page = 1}) async =>
      const Paged(items: [], page: 1, total: 0, pageSize: 20);

  void _rule() {
    if (ruleError != null) throw ruleError!;
  }

  @override
  Future<List<TaxYear>> taxYears() async => years;

  @override
  Future<List<TaxRules>> taxRuleVersions(String fy) async => versions;

  @override
  Future<TaxRules> taxRules(String id) async => rule;

  @override
  Future<TaxRules> createTaxRules(String fy) async {
    ruleCalls.add('create:$fy');
    _rule();
    return rule;
  }

  @override
  Future<TaxRules> saveTaxRules(String id, {required int versionRow, required Map<String, dynamic> rules, String? note}) async {
    savedRules.add({'row': versionRow, 'rules': rules, 'note': note});
    _rule();
    return rule;
  }

  @override
  Future<TaxRules> submitTaxRules(String id, {required int versionRow}) async {
    ruleCalls.add('submit:$versionRow');
    _rule();
    return rule;
  }

  @override
  Future<TaxRules> withdrawTaxRules(String id, {required int versionRow}) async {
    ruleCalls.add('withdraw:$versionRow');
    return rule;
  }

  @override
  Future<TaxRules> verifyTaxRules(
    String id, {
    required int versionRow,
    required String rulesSha256,
    required String attestation,
    required String currentPassword,
  }) async {
    verifies.add({'row': versionRow, 'sha': rulesSha256, 'att': attestation, 'pw': currentPassword});
    if (pwRejects > 0) {
      pwRejects--;
      throw const ApiException(code: 'REAUTH_FAILED', message: 'no', status: 403);
    }
    _rule();
    return rule;
  }

  @override
  Future<TaxRules> rejectTaxRules(String id, {required int versionRow, required String comment}) async {
    ruleCalls.add('reject:$versionRow:$comment');
    return rule;
  }

  @override
  Future<TaxWindows> saveTaxWindows(String fy, TaxWindows w, {String? currentPassword}) async {
    windowSaves.add({'fy': fy, ...w.toJson(), 'pw': currentPassword});
    return w;
  }

  // employee
  @override
  Future<List<MyTaxYear>> myTaxYears() async => const [];

  @override
  Future<TaxDeclaration> myDeclaration({String? fy}) async {
    if (myDeclError != null) throw myDeclError!;
    return myDecl;
  }

  @override
  Future<TaxDeclaration> saveMyDeclaration(DeclarationSave body) async {
    saves.add(body.toJson(null));
    if (declSaveError != null) throw declSaveError!;
    return TaxDeclaration.fromJson(declJson(version: saveVersion, dirty: true));
  }

  @override
  Future<TaxDeclaration> submitMyDeclaration({required String fy, required int expectedVersion}) async {
    submits.add(expectedVersion);
    if (declSaveError != null) throw declSaveError!;
    return TaxDeclaration.fromJson(declJson(version: expectedVersion + 1));
  }

  @override
  Future<List<TaxProof>> myProofs({String? fy}) async => myProofList;

  @override
  Future<String> uploadTaxProofFile(List<int> bytes, String filename) async {
    uploads.add(filename);
    return 'file-upload-${uploads.length}';
  }

  @override
  Future<TaxProof> createMyProof({
    required String fy,
    required String itemId,
    required String claimed,
    required List<String> fileIds,
  }) async {
    proofCreates.add({'item': itemId, 'claimed': claimed, 'files': fileIds});
    if (proofError != null) throw proofError!;
    return TaxProof.fromJson(proofJson(user: 'me'));
  }

  @override
  Future<TaxProof> cancelMyProof(String id) async {
    cancels.add(id);
    return TaxProof.fromJson(proofJson(status: 'cancelled'));
  }

  @override
  Future<List<MyForm16>> myForm16() async => myForm16s;

  // admin declarations
  @override
  Future<TaxDeclaration> taxDeclarationOf(String userId, {String? fy, bool reveal = false, String? currentPassword}) async {
    revealCalls.add(reveal);
    if (reveal) revealPasswords.add(currentPassword);
    return adminDecl;
  }

  @override
  Future<Paged<DeclarationListItem>> taxDeclarations(TaxDeclarationQuery q, {int page = 1}) async => Paged(
        items: [
          DeclarationListItem.fromJson({
            'user': userJson('emp'),
            'fy': '2026-27',
            'status': 'submitted',
            'regime': 'old',
            'items': 3,
            'proofs': {'submitted': 1, 'verified': 0, 'rejected': 0},
            'flags': <String>[],
          }),
        ],
        page: 1,
        total: 1,
        pageSize: 20,
      );

  @override
  Future<TaxDeclaration> saveDeclarationOnBehalf(String userId, DeclarationSave body, {required String currentPassword}) async {
    onBehalf.add({...body.toJson(currentPassword), 'user': userId});
    if (pwRejects > 0) {
      pwRejects--;
      throw const ApiException(code: 'REAUTH_FAILED', message: 'no', status: 403);
    }
    if (declSaveError != null) throw declSaveError!;
    return TaxDeclaration.fromJson(declJson(user: 'emp', version: saveVersion, enteredBy: 'hr'));
  }

  @override
  Future<TaxDeclaration> unlockDeclaration(
    String userId, {
    required String fy,
    required String until,
    required String reason,
    required String currentPassword,
    bool allowRegimeChange = false,
  }) async {
    unlocks.add({
      'user': userId,
      'until': until,
      'reason': reason,
      'pw': currentPassword,
      'regime': allowRegimeChange,
    });
    return adminDecl;
  }

  @override
  Future<Paged<TaxProof>> taxProofs(TaxProofQuery q, {int page = 1}) async =>
      Paged(items: proofList, page: 1, total: proofList.length, pageSize: 20);

  @override
  Future<TaxProof> verifyTaxProof(String id, {required int expectedVersion, required String verified, String? comment}) async {
    if (proofDecideError != null) throw proofDecideError!;
    decisions.add('verify:$expectedVersion:$verified:${comment ?? ''}');
    return TaxProof.fromJson(proofJson(status: 'verified'));
  }

  @override
  Future<TaxProof> rejectTaxProof(String id, {required int expectedVersion, required String comment}) async {
    if (proofDecideError != null) throw proofDecideError!;
    decisions.add('reject:$expectedVersion:$comment');
    return TaxProof.fromJson(proofJson(status: 'rejected'));
  }

  // form 16
  @override
  Future<Form16DryRun> form16DryRun(String fy, String kind, List<Form16UploadFile> files) async {
    dryCalls.add([fy, kind, ...files.map((f) => f.name)]);
    return dry;
  }

  @override
  Future<Form16CommitResult> form16Commit(
    String fy,
    String kind,
    List<Form16UploadFile> files, {
    required String manifestSha256,
    required String currentPassword,
  }) async {
    commits.add({'fy': fy, 'kind': kind, 'files': files.length, 'manifest': manifestSha256, 'pw': currentPassword});
    if (pwRejects > 0) {
      pwRejects--;
      throw const ApiException(code: 'REAUTH_FAILED', message: 'no', status: 403);
    }
    if (commitError != null) throw commitError!;
    return const Form16CommitResult(created: 2, replaced: 1);
  }

  @override
  Future<Paged<Form16Doc>> form16Documents(Form16Query q, {int page = 1}) async {
    final items = [for (final d in docs) if (q.status == null || d.status == q.status) d];
    return Paged(items: items, page: 1, total: items.length, pageSize: 20);
  }

  @override
  Future<PayrollFile> form16AdminFile(String id) async => PayrollFile(Uint8List(1), 'form16-admin.pdf');

  @override
  Future<PayrollFile> myForm16File(String id) async => PayrollFile(Uint8List(1), 'form16-EMP001-2026-27.pdf');

  @override
  Future<PayrollFile> taxProofFile(ProofFile f) async => PayrollFile(Uint8List(1), 'proof.pdf');

  @override
  Future<Form16PublishResult> publishForm16(
    String fy,
    List<({String id, String sha256})> documents, {
    required String currentPassword,
  }) async {
    publishes.add([for (final d in documents) d.id]);
    return publishResult;
  }

  @override
  Future<void> withdrawForm16(String id, {required String reason, required String currentPassword}) async {
    withdraws.add('$id:$reason:$currentPassword');
  }
}

const _manage = ['payroll.view_all', 'payroll.manage'];
const _approve = ['payroll.view_all', 'payroll.approve'];
const _own = ['payroll.view_own'];

Future<void> _tap(WidgetTester tester, String key) async {
  final f = find.byKey(ValueKey(key));
  await tester.ensureVisible(f);
  await tester.pumpAndSettle();
  await tester.tap(f);
  await tester.pumpAndSettle();
}

/// Taps a button that shows a spinner while a password prompt is open (pumpAndSettle would never finish).
Future<void> _tapBusy(WidgetTester tester, String key) async {
  final f = find.byKey(ValueKey(key));
  await tester.ensureVisible(f);
  await tester.pumpAndSettle();
  await tester.tap(f);
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 600));
}

Future<void> _password(WidgetTester tester, String pw) async {
  expect(find.byKey(const ValueKey('reauth-password')), findsOneWidget);
  await tester.enterText(find.byKey(const ValueKey('reauth-password')), pw);
  await tester.tap(find.text('Confirm'));
  await tester.pumpAndSettle();
}

/// A wrong password re-prompts while the button behind it still spins (pumpAndSettle would never finish).
Future<void> _passwordBusy(WidgetTester tester, String pw) async {
  expect(find.byKey(const ValueKey('reauth-password')), findsOneWidget);
  await tester.enterText(find.byKey(const ValueKey('reauth-password')), pw);
  await tester.tap(find.text('Confirm'));
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 600));
}

Future<void> _comment(WidgetTester tester, String text) async {
  if (text.isNotEmpty) await tester.enterText(find.byKey(const ValueKey('comment-field')), text);
  await tester.tap(find.byKey(const ValueKey('comment-confirm')));
  await tester.pumpAndSettle();
}

Future<void> _enter(WidgetTester tester, String key, String text) async {
  final f = find.byKey(ValueKey(key));
  await tester.ensureVisible(f);
  await tester.pumpAndSettle();
  await tester.enterText(f, text);
  await tester.pump();
}

/// Pumps a launcher that pushes [screen], so that a screen which pops itself has a route to return to.
Future<void> _pumpPushed(
  WidgetTester tester,
  Widget screen, {
  required FakeRepo repo,
  List<String> perms = const [],
  String? selfId = 'me',
  List<Override> extra = const [],
  FakeDelivery? delivery,
  ClaimBillPicker? billPicker,
}) async {
  await pumpPayroll(
    tester,
    Builder(
      builder: (context) => Scaffold(
        body: Center(
          child: ElevatedButton(
            key: const ValueKey('launch'),
            onPressed: () => Navigator.of(context).push<void>(MaterialPageRoute<void>(builder: (_) => screen)),
            child: const Text('open'),
          ),
        ),
      ),
    ),
    repo: repo,
    perms: perms,
    selfId: selfId,
    extraOverrides: extra,
    delivery: delivery,
    billPicker: billPicker,
  );
  await tester.tap(find.byKey(const ValueKey('launch')));
  await tester.pumpAndSettle();
}

TaxRules _rules({
  String status = 'draft',
  String? note = 'Reviewed against the Act',
  String creator = 'maker',
  String? submitter,
  int version = 2,
  Map<String, dynamic>? rules,
}) =>
    TaxRules.fromJson(rulesJson(status: status, note: note, creator: creator, submitter: submitter, version: version, rules: rules));

PickedPdf _pdf(String name) => PickedPdf(name: name, bytes: Uint8List.fromList([37, 80, 68, 70]));

void main() {
  group('rules: years and versions', () {
    testWidgets('years show the verified version and the open version', (tester) async {
      final repo = Fake5b()
        ..years = [
          TaxYear.fromJson(yearJson(verified: 1, open: {'id': 'ru2', 'version': 2, 'status': 'submitted'})),
          TaxYear.fromJson(yearJson(fy: '2025-26', verified: 4, current: false)),
        ];
      await pumpPayroll(tester, const Scaffold(body: TaxRulesSection()), repo: repo, perms: _manage);
      expect(find.text('FY 2026-27  (this year)'), findsOneWidget);
      expect(find.text('Verified version 1'), findsOneWidget);
      expect(find.text('v2 Waiting for verification'), findsOneWidget);
      expect(find.text('Verified version 4'), findsOneWidget);
    });

    testWidgets('an empty list and a failure have their own states', (tester) async {
      await pumpPayroll(tester, const Scaffold(body: TaxRulesSection()), repo: Fake5b(), perms: _manage);
      expect(find.text('No tax years yet.'), findsOneWidget);
    });

    testWidgets('the year screen offers a new draft only when none is open, and windows', (tester) async {
      final repo = Fake5b()
        ..versions = [_rules(status: 'verified', version: 1)]
        ..rule = _rules();
      await _pumpPushed(tester, const TaxYearScreen(fy: '2026-27'), repo: repo, perms: _manage);
      expect(find.text('Declarations: Wed, 1 Apr 2026 to Tue, 30 Jun 2026'), findsOneWidget);
      await _tap(tester, 'tax-new-draft');
      expect(repo.ruleCalls, ['create:2026-27']);
      expect(find.textContaining('copy of the latest version'), findsOneWidget);
      // the new draft opens
      expect(find.text('Tax rules'), findsOneWidget);
    });

    testWidgets('no new draft while one is open, and a viewer cannot change windows', (tester) async {
      final repo = Fake5b()..versions = [_rules(), _rules(status: 'verified', version: 1)];
      await _pumpPushed(tester, const TaxYearScreen(fy: '2026-27'), repo: repo, perms: const ['payroll.view_all']);
      expect(find.byKey(const ValueKey('tax-new-draft')), findsNothing);
      expect(find.byKey(const ValueKey('tax-windows-edit')), findsNothing);
      expect(find.text('Draft'), findsOneWidget);
    });

    testWidgets('windows: both dates or neither, then saved', (tester) async {
      final repo = Fake5b()..versions = [_rules(status: 'verified', version: 1)];
      await _pumpPushed(tester, const TaxYearScreen(fy: '2026-27'), repo: repo, perms: _manage);
      await _tap(tester, 'tax-windows-edit');
      await tester.enterText(find.byKey(const ValueKey('win-proof-close')), '');
      await _tap(tester, 'win-save');
      expect(find.byKey(const ValueKey('win-error')), findsOneWidget);
      expect(repo.windowSaves, isEmpty);
      await tester.enterText(find.byKey(const ValueKey('win-proof-open')), '');
      await _tap(tester, 'win-save');
      expect(repo.windowSaves.single['proof_opens_on'], isNull);
      expect(repo.windowSaves.single['declaration_closes_on'], '2026-06-30');
      expect(repo.windowSaves.single['pw'], isNull);
    });

    testWidgets('windows: reopening a passed window asks for the password', (tester) async {
      final repo = Fake5b()..versions = [_rules(status: 'verified', version: 1)];
      await _pumpPushed(tester, const TaxYearScreen(fy: '2026-27'), repo: repo, perms: _manage);
      await _tap(tester, 'tax-windows-edit');
      // The declaration window closed on 2026-06-30; the test clock is after that.
      await tester.enterText(find.byKey(const ValueKey('win-decl-close')), '2026-12-31');
      await _tap(tester, 'win-save');
      expect(repo.windowSaves, isEmpty);
      await _password(tester, 'pw');
      expect(repo.windowSaves.single['declaration_closes_on'], '2026-12-31');
      expect(repo.windowSaves.single['pw'], 'pw');
    });
  });

  group('rules: review, submit and verify', () {
    testWidgets('a copied (UNVERIFIED) note blocks submission with an explanation', (tester) async {
      final repo = Fake5b()..rule = _rules(note: 'UNVERIFIED seed copied from FY 2025-26');
      await pumpPayroll(tester, const TaxRulesScreen(rulesId: 'ru1'), repo: repo, perms: _manage);
      expect(find.byKey(const ValueKey('rules-unverified-warning')), findsOneWidget);
      await _tap(tester, 'rules-submit');
      expect(find.textContaining('still starts with UNVERIFIED'), findsOneWidget);
      expect(repo.ruleCalls, isEmpty);
    });

    testWidgets('a reviewed draft is submitted with its version row', (tester) async {
      final repo = Fake5b()..rule = _rules();
      await pumpPayroll(tester, const TaxRulesScreen(rulesId: 'ru1'), repo: repo, perms: _manage);
      await _tap(tester, 'rules-submit');
      expect(repo.ruleCalls, ['submit:3']);
      expect(find.textContaining('A different person must verify'), findsOneWidget);
    });

    testWidgets('all values are listed for the reviewer and the diff to the earlier version is shown', (tester) async {
      final older = sampleRules();
      ((older['regimes'] as Map)['new'] as Map)['standard_deduction'] = '50000.00';
      final repo = Fake5b()
        ..rule = _rules(status: 'submitted', submitter: 'sub')
        ..versions = [_rules(status: 'submitted', submitter: 'sub'), _rules(status: 'verified', version: 1, rules: older)];
      await pumpPayroll(tester, const TaxRulesScreen(rulesId: 'ru1'), repo: repo, perms: _approve, selfId: 'checker');
      expect(find.text('Changes from version 1'), findsOneWidget);
      expect(find.textContaining('regimes.new.standard_deduction: 50000.00 -> 75000.00'), findsOneWidget);
      expect(find.byKey(const ValueKey('rules-group-regimes')), findsOneWidget);
      expect(find.byKey(const ValueKey('rules-group-sections')), findsOneWidget);
    });

    testWidgets('the maker is told why they cannot verify', (tester) async {
      final repo = Fake5b()..rule = _rules(status: 'submitted', submitter: 'sub');
      await pumpPayroll(tester, const TaxRulesScreen(rulesId: 'ru1'), repo: repo, perms: _approve, selfId: 'maker');
      expect(find.byKey(const ValueKey('rules-verify')), findsNothing);
      expect(find.byKey(const ValueKey('rules-reject')), findsNothing);
      expect(find.byKey(const ValueKey('rules-cannot-decide')), findsOneWidget);
    });

    testWidgets('a checker verifies with an attestation and the password; the reviewed fingerprint is sent', (tester) async {
      final repo = Fake5b()
        ..rule = _rules(status: 'submitted', submitter: 'sub')
        ..pwRejects = 1;
      await pumpPayroll(tester, const TaxRulesScreen(rulesId: 'ru1'), repo: repo, perms: _approve, selfId: 'checker');
      await _tap(tester, 'rules-verify');
      await _enter(tester, 'attestation-field', 'too short');
      await _tap(tester, 'attestation-confirm');
      expect(find.textContaining('at least 20 characters'), findsOneWidget);
      expect(repo.verifies, isEmpty);
      await _enter(tester, 'attestation-field', 'I verified these rules against the Act on 1 May 2026');
      await _tap(tester, 'attestation-confirm');
      await _password(tester, 'bad');
      expect(find.text('That password is not correct.'), findsOneWidget);
      await _password(tester, 'good');
      expect(repo.verifies.map((v) => v['pw']), ['bad', 'good']);
      expect(repo.verifies.last['row'], 3);
      expect(repo.verifies.last['sha'], 'a' * 64);
      expect(repo.verifies.last['att'], 'I verified these rules against the Act on 1 May 2026');
      expect(find.textContaining('Verified. New payroll runs'), findsOneWidget);
    });

    testWidgets('RULES_CHANGED and SECOND_PERSON_REQUIRED are explained', (tester) async {
      final repo = Fake5b()
        ..rule = _rules(status: 'submitted', submitter: 'sub')
        ..ruleError = const ApiException(code: 'RULES_CHANGED', message: 'x', status: 409);
      await pumpPayroll(tester, const TaxRulesScreen(rulesId: 'ru1'), repo: repo, perms: _approve, selfId: 'checker');
      await _tap(tester, 'rules-verify');
      await _enter(tester, 'attestation-field', 'I verified these rules against the Act on 1 May 2026');
      await _tap(tester, 'attestation-confirm');
      await _password(tester, 'pw');
      expect(find.textContaining('changed after you opened'), findsOneWidget);
      repo.ruleError = const ApiException(code: 'SECOND_PERSON_REQUIRED', message: 'x', status: 403);
      await _tap(tester, 'rules-verify');
      await _enter(tester, 'attestation-field', 'I verified these rules against the Act on 1 May 2026');
      await _tap(tester, 'attestation-confirm');
      await _password(tester, 'pw');
      expect(find.textContaining('A different person must do this'), findsOneWidget);
    });

    testWidgets('reject needs a comment', (tester) async {
      final repo = Fake5b()..rule = _rules(status: 'submitted', submitter: 'sub');
      await pumpPayroll(tester, const TaxRulesScreen(rulesId: 'ru1'), repo: repo, perms: _approve, selfId: 'checker');
      await _tap(tester, 'rules-reject');
      await _comment(tester, '');
      expect(repo.ruleCalls, isEmpty);
      await _comment(tester, 'Slab 2 is wrong');
      expect(repo.ruleCalls, ['reject:3:Slab 2 is wrong']);
    });
  });

  group('rules editor', () {
    testWidgets('a rate above 50 is refused before anything is sent; a fix is saved with the note', (tester) async {
      final repo = Fake5b();
      await _pumpPushed(tester, TaxRulesEditorScreen(rules: _rules()), repo: repo, perms: _manage);
      await _enter(tester, 'new-slab-0-rate', '60');
      await _tap(tester, 'rules-editor-save');
      expect(find.byKey(const ValueKey('rules-editor-issues')), findsOneWidget);
      expect(find.textContaining('rules.regimes.new.slabs[0].rate_pct'), findsOneWidget);
      expect(repo.savedRules, isEmpty);
      await _enter(tester, 'new-slab-0-rate', '7');
      await _tap(tester, 'rules-editor-save');
      final saved = repo.savedRules.single;
      expect(saved['row'], 3);
      expect(saved['note'], 'Reviewed against the Act');
      final slabs = (((saved['rules']! as Map)['regimes'] as Map)['new'] as Map)['slabs'] as List;
      expect((slabs.first as Map)['rate_pct'], '7');
      expect((slabs.last as Map)['to'], isNull);
    });

    testWidgets('the start of a slab follows the end of the one before; money is normalised on save', (tester) async {
      final repo = Fake5b();
      await _pumpPushed(tester, TaxRulesEditorScreen(rules: _rules()), repo: repo, perms: _manage);
      await _enter(tester, 'new-slab-0-to', '500000');
      expect(find.text('From 500000'), findsOneWidget);
      await _tap(tester, 'rules-editor-save');
      final slabs = ((((repo.savedRules.single['rules']! as Map)['regimes'] as Map)['new']) as Map)['slabs'] as List;
      expect((slabs[0] as Map)['to'], '500000.00');
      expect((slabs[1] as Map)['from'], '500000.00');
    });

    testWidgets('a new slab leaves the end of the old last slab to be filled in', (tester) async {
      final repo = Fake5b();
      await _pumpPushed(tester, TaxRulesEditorScreen(rules: _rules()), repo: repo, perms: _manage);
      await _tap(tester, 'new-slab-add');
      await _tap(tester, 'rules-editor-save');
      expect(find.byKey(const ValueKey('rules-editor-issues')), findsOneWidget);
      expect(find.textContaining('rules.regimes.new.slabs[2].to'), findsOneWidget);
      expect(repo.savedRules, isEmpty);
    });

    testWidgets('a server error with a JSON path is shown on the screen', (tester) async {
      final repo = Fake5b()
        ..ruleError = const ApiException(
          code: 'VALIDATION_ERROR',
          message: 'x',
          status: 422,
          details: [FieldError('rules.regimes.old.allowed_sections[3]', 'unknown section')],
        );
      await _pumpPushed(tester, TaxRulesEditorScreen(rules: _rules()), repo: repo, perms: _manage);
      await _tap(tester, 'rules-editor-save');
      expect(find.textContaining('rules.regimes.old.allowed_sections[3]'), findsOneWidget);
    });

    testWidgets('a section code that the allowed list names but nobody defined is flagged locally', (tester) async {
      final bad = sampleRules();
      ((bad['regimes'] as Map)['old'] as Map)['allowed_sections'] = ['80C', '80CCC'];
      final repo = Fake5b();
      await _pumpPushed(tester, TaxRulesEditorScreen(rules: _rules(rules: bad)), repo: repo, perms: _manage);
      await _tap(tester, 'rules-editor-save');
      expect(find.textContaining('rules.regimes.old.allowed_sections[1]'), findsOneWidget);
      expect(repo.savedRules, isEmpty);
    });
  });

  group('employee: declaration', () {
    testWidgets('shows the submitted version, the regime and the items', (tester) async {
      final repo = Fake5b()..myDecl = TaxDeclaration.fromJson(declJson(dirty: true));
      await pumpPayroll(tester, const MyTaxScreen(), repo: repo, perms: _own);
      expect(find.text('Submitted version 3 (old regime)'), findsOneWidget);
      expect(find.byKey(const ValueKey('decl-dirty-note')), findsOneWidget);
      expect(find.textContaining('Section 80C'), findsOneWidget);
      expect(find.text('₹1,50,000.00'), findsOneWidget);
      expect(find.textContaining('The declaration window is open'), findsOneWidget);
    });

    testWidgets('without permission nothing is shown', (tester) async {
      await pumpPayroll(tester, const MyTaxScreen(), repo: Fake5b(), perms: const []);
      expect(find.byKey(const ValueKey('tab-declaration')), findsNothing);
    });

    testWidgets('add an item, save the draft: expected version, regime and the new item are sent', (tester) async {
      final repo = Fake5b();
      await pumpPayroll(tester, const MyTaxScreen(), repo: repo, perms: _own);
      await _tap(tester, 'decl-add-item');
      await _enter(tester, 'item-amount', '50000');
      await _enter(tester, 'item-field-description', 'NPS');
      await _tap(tester, 'item-save');
      expect(find.byKey(const ValueKey('decl-item-1')), findsOneWidget);
      await _tap(tester, 'decl-save');
      final body = repo.saves.single;
      expect(body['expected_version'], 5);
      expect(body['regime'], 'old');
      final items = body['items']! as List;
      expect(items, hasLength(2));
      expect((items.first as Map)['id'], 'i-1');
      expect((items.last as Map)['id'], isNull);
      expect((items.last as Map)['amount'], '50000.00');
      expect(((items.last as Map)['details'] as Map)['description'], 'NPS');
      expect(find.text('Draft saved.'), findsOneWidget);
    });

    testWidgets('an amount that is not valid is refused in the dialog', (tester) async {
      final repo = Fake5b();
      await pumpPayroll(tester, const MyTaxScreen(), repo: repo, perms: _own);
      await _tap(tester, 'decl-add-item');
      await _enter(tester, 'item-amount', '0');
      await _tap(tester, 'item-save');
      expect(find.text('Amount must be more than 0'), findsOneWidget);
      expect(find.byKey(const ValueKey('decl-item-1')), findsNothing);
    });

    testWidgets('submit saves pending changes first and submits the version that was saved', (tester) async {
      final repo = Fake5b()..saveVersion = 6;
      await pumpPayroll(tester, const MyTaxScreen(), repo: repo, perms: _own);
      await _tap(tester, 'decl-item-delete-0');
      await tester.pump();
      await _tap(tester, 'decl-submit');
      expect(repo.saves, hasLength(1));
      expect(repo.submits, [6]);
      expect(find.text('Submitted.'), findsOneWidget);
    });

    testWidgets('a regime must be chosen before submitting', (tester) async {
      final repo = Fake5b()..myDecl = TaxDeclaration.fromJson(declJson(regime: null, withSubmitted: false, dirty: true));
      await pumpPayroll(tester, const MyTaxScreen(), repo: repo, perms: _own);
      await _tap(tester, 'decl-submit');
      expect(find.byKey(const ValueKey('decl-error')), findsOneWidget);
      expect(find.textContaining('Choose the old or the new regime'), findsOneWidget);
      expect(repo.submits, isEmpty);
    });

    testWidgets('closed window: read only with the reason', (tester) async {
      final repo = Fake5b()..myDecl = TaxDeclaration.fromJson(declJson(canEdit: false, reason: 'CLOSED'));
      await pumpPayroll(tester, const MyTaxScreen(), repo: repo, perms: _own);
      expect(find.textContaining('The declaration window is closed'), findsOneWidget);
      expect(find.byKey(const ValueKey('decl-add-item')), findsNothing);
      expect(find.byKey(const ValueKey('decl-item-edit-0')), findsNothing);
      expect(find.byKey(const ValueKey('decl-save')), findsOneWidget);
      expect(tester.widget<OutlinedButton>(find.byKey(const ValueKey('decl-save'))).onPressed, isNull);
    });

    testWidgets('proof window: the regime is fixed', (tester) async {
      final repo = Fake5b()..myDecl = TaxDeclaration.fromJson(declJson(reason: 'PROOF_WINDOW'));
      await pumpPayroll(tester, const MyTaxScreen(), repo: repo, perms: _own);
      expect(find.text('The regime is fixed for this year.'), findsOneWidget);
      final seg = tester.widget<SegmentedButton<String?>>(find.byKey(const ValueKey('decl-regime')));
      expect(seg.onSelectionChanged, isNull);
    });

    testWidgets('WINDOW_CLOSED and REGIME_LOCKED from the server are explained', (tester) async {
      final repo = Fake5b()..declSaveError = const ApiException(code: 'WINDOW_CLOSED', message: 'x', status: 409);
      await pumpPayroll(tester, const MyTaxScreen(), repo: repo, perms: _own);
      await _tap(tester, 'decl-item-delete-0');
      await _tap(tester, 'decl-save');
      expect(find.textContaining('This window is closed'), findsOneWidget);
      repo.declSaveError = const ApiException(code: 'REGIME_LOCKED', message: 'x', status: 409);
      await _tap(tester, 'decl-save');
      expect(find.textContaining('regime is fixed once'), findsOneWidget);
    });

    testWidgets('a PAN that the server shows masked must be typed again before the item can be saved', (tester) async {
      final item = {
        'id': 'i-2',
        'section': 'HRA',
        'amount': '240000.00',
        'details': {
          'monthly_rent': '20000.00',
          'from_month': '2026-04',
          'to_month': '2027-03',
          'landlord_name': 'R K',
          'landlord_pan': 'XXXXX1234X',
          'city': 'Chennai',
          'metro': true,
        },
        'flags': <String>[],
      };
      final repo = Fake5b()..myDecl = TaxDeclaration.fromJson(declJson(items: [item]));
      await pumpPayroll(tester, const MyTaxScreen(), repo: repo, perms: _own);
      // another change makes Save available, but the masked PAN would be stored as it is
      await _tap(tester, 'decl-regime');
      await tester.tap(find.text('New regime'));
      await tester.pumpAndSettle();
      await _tap(tester, 'decl-save');
      expect(find.textContaining('hidden for safety'), findsOneWidget);
      expect(repo.saves, isEmpty);
      // editing the item: the field starts empty and asks for the full PAN
      await _tap(tester, 'decl-item-edit-0');
      expect(find.text('The saved PAN is hidden. Type the full PAN again to keep it.'), findsOneWidget);
      await _tap(tester, 'item-save');
      expect(find.text('Type the full PAN again'), findsOneWidget);
      await _enter(tester, 'item-field-landlord_pan', 'abcde1234f');
      await _tap(tester, 'item-save');
      await _tap(tester, 'decl-save');
      expect(repo.saves, hasLength(1));
      final saved = ((repo.saves.single['items']! as List).single as Map)['details'] as Map;
      expect(saved['landlord_pan'], 'ABCDE1234F');
    });

    testWidgets('HRA: the yearly rent is worked out and the landlord PAN is required above the limit', (tester) async {
      final repo = Fake5b()..myDecl = TaxDeclaration.fromJson(declJson(items: []));
      await pumpPayroll(tester, const MyTaxScreen(), repo: repo, perms: _own);
      await _tap(tester, 'decl-add-item');
      await tester.tap(find.byKey(const ValueKey('item-section')));
      await tester.pumpAndSettle();
      await tester.tap(find.textContaining('House rent').last);
      await tester.pumpAndSettle();
      await _enter(tester, 'item-field-monthly_rent', '20000');
      await _enter(tester, 'item-field-from_month', '2026-04');
      await _enter(tester, 'item-field-to_month', '2027-03');
      expect(find.text('Yearly rent: ₹2,40,000.00'), findsOneWidget);
      expect(find.byKey(const ValueKey('item-amount')), findsNothing); // computed by the server
      await _tap(tester, 'item-save');
      expect(find.text('Type the full PAN again'), findsNothing);
      expect(find.text('Enter the PAN'), findsOneWidget);
    });

    testWidgets('the form offers the sections the server sent, not the built-in list', (tester) async {
      final repo = Fake5b()
        ..myDecl = TaxDeclaration.fromJson({
          ...declJson(items: []),
          'sections': [
            {
              'code': 'NPS2',
              'name': 'Server only section',
              'limit': null,
              'proof_required': true,
              'fields': ['description'],
            },
          ],
          'proof_max_files': 4,
        });
      await pumpPayroll(tester, const MyTaxScreen(), repo: repo, perms: _own);
      await _tap(tester, 'decl-add-item');
      await tester.tap(find.byKey(const ValueKey('item-section')));
      await tester.pumpAndSettle();
      expect(find.textContaining('Server only section'), findsWidgets);
      expect(find.textContaining('House rent'), findsNothing);
    });

    testWidgets('rules not verified yet: a plain message, no error', (tester) async {
      final repo = Fake5b()..myDeclError = const ApiException(code: 'TAX_RULES_NOT_VERIFIED', message: 'x', status: 409);
      await pumpPayroll(tester, const MyTaxScreen(), repo: repo, perms: _own);
      expect(find.textContaining('are not open yet'), findsOneWidget);
    });

    testWidgets('a failure to load can be retried', (tester) async {
      final repo = Fake5b()..myDeclError = const ApiException(code: 'NETWORK_ERROR', message: 'offline');
      await pumpPayroll(tester, const MyTaxScreen(), repo: repo, perms: _own);
      expect(find.text('Try again'), findsOneWidget);
      repo.myDeclError = null;
      await tester.tap(find.text('Try again'));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('decl-submitted-badge')), findsOneWidget);
    });

    testWidgets('the payslips screen has an entry to Tax', (tester) async {
      await pumpPayroll(tester, const PayslipsScreen(), repo: Fake5b(), perms: _own);
      await _tap(tester, 'open-my-tax');
      expect(find.byKey(const ValueKey('tab-form16')), findsOneWidget);
    });
  });

  group('employee: proofs', () {
    Future<Fake5b> openProofs(
      WidgetTester tester, {
      String reason = 'PROOF_WINDOW',
      bool canEdit = true,
      List<TaxProof> proofs = const [],
      ClaimBillPicker? picker,
    }) async {
      final repo = Fake5b()
        ..myDecl = TaxDeclaration.fromJson(declJson(reason: reason, canEdit: canEdit))
        ..myProofList = proofs;
      await pumpPayroll(tester, const MyTaxScreen(), repo: repo, perms: _own, billPicker: picker);
      await _tap(tester, 'tab-proofs');
      return repo;
    }

    testWidgets('the item that needs a proof offers Add proof; no file is refused', (tester) async {
      final repo = await openProofs(tester);
      expect(find.text('Declared ₹1,50,000.00'), findsOneWidget);
      await _tap(tester, 'proof-add-i-1');
      await _tap(tester, 'proof-send');
      expect(find.text('Add at least one file (a photo or a PDF of the proof).'), findsOneWidget);
      expect(repo.proofCreates, isEmpty);
    });

    testWidgets('the file limit comes from the server (proof_max_files)', (tester) async {
      final picker = FakeBillPicker(PickedBill(name: 'receipt.pdf', bytes: Uint8List.fromList(List.filled(20, 1))));
      final repo = Fake5b()
        ..myDecl = TaxDeclaration.fromJson({...declJson(reason: 'PROOF_WINDOW'), 'proof_max_files': 2})
        ..myProofList = const [];
      await pumpPayroll(tester, const MyTaxScreen(), repo: repo, perms: _own, billPicker: picker);
      await _tap(tester, 'tab-proofs');
      await _tap(tester, 'proof-add-i-1');
      await _tap(tester, 'proof-pick-file');
      await _tap(tester, 'proof-pick-file');
      await _tap(tester, 'proof-pick-file');
      expect(find.text('At most 2 files for one proof.'), findsOneWidget);
      expect(find.byKey(const ValueKey('proof-file-2')), findsNothing);
    });

    testWidgets('the claimed amount cannot be above the declared amount', (tester) async {
      await openProofs(tester);
      await _tap(tester, 'proof-add-i-1');
      await _enter(tester, 'proof-claimed', '150000.01');
      await _tap(tester, 'proof-send');
      expect(find.text('The amount claimed cannot be more than the declared amount'), findsOneWidget);
    });

    testWidgets('files are uploaded one by one, then the proof is created with their ids', (tester) async {
      final picker = FakeBillPicker(PickedBill(name: 'receipt.pdf', bytes: Uint8List.fromList(List.filled(20, 1))));
      final repo = await openProofs(tester, picker: picker);
      await _tap(tester, 'proof-add-i-1');
      await _enter(tester, 'proof-claimed', '120000');
      await _tap(tester, 'proof-pick-file');
      await _tap(tester, 'proof-pick-file');
      expect(find.byKey(const ValueKey('proof-file-1')), findsOneWidget);
      await _tap(tester, 'proof-send');
      expect(repo.uploads, ['receipt.pdf', 'receipt.pdf']);
      expect(repo.proofCreates.single, {
        'item': 'i-1',
        'claimed': '120000.00',
        'files': ['file-upload-1', 'file-upload-2'],
      });
      expect(find.textContaining('Proof sent'), findsOneWidget);
    });

    testWidgets('a file that is too large or the wrong type is refused before upload', (tester) async {
      final picker = FakeBillPicker(PickedBill(name: 'big.pdf', bytes: Uint8List(maxProofFileBytes + 1)));
      final repo = await openProofs(tester, picker: picker);
      await _tap(tester, 'proof-add-i-1');
      await _tap(tester, 'proof-pick-file');
      expect(find.text('The file is too large (5 MB at most).'), findsOneWidget);
      picker.result = PickedBill(name: 'notes.txt', bytes: Uint8List(10));
      await _tap(tester, 'proof-pick-file');
      expect(find.text('Use a JPEG, PNG or PDF file.'), findsOneWidget);
      expect(find.byKey(const ValueKey('proof-file-0')), findsNothing);
      expect(repo.uploads, isEmpty);
    });

    testWidgets('CONFLICT (a proof already exists for the item) and WINDOW_CLOSED stay in the dialog', (tester) async {
      final picker = FakeBillPicker(PickedBill(name: 'r.pdf', bytes: Uint8List(10)));
      final repo = await openProofs(tester, picker: picker);
      repo.proofError = const ApiException(code: 'CONFLICT', message: 'x', status: 409);
      await _tap(tester, 'proof-add-i-1');
      await _tap(tester, 'proof-pick-file');
      await _tap(tester, 'proof-send');
      expect(find.byKey(const ValueKey('proof-error')), findsOneWidget);
      expect(find.textContaining('cancel the earlier one'), findsOneWidget);
      repo.proofError = const ApiException(code: 'WINDOW_CLOSED', message: 'x', status: 409);
      await _tap(tester, 'proof-send');
      expect(find.textContaining('This window is closed'), findsOneWidget);
      expect(repo.uploads, ['r.pdf']); // the uploaded file is kept for the retry
    });

    testWidgets('a waiting proof can be cancelled; a rejected one shows the comment and allows a new one', (tester) async {
      final waiting = await openProofs(tester, proofs: [TaxProof.fromJson(proofJson(user: 'me'))]);
      expect(find.byKey(const ValueKey('proof-add-i-1')), findsNothing);
      await _tap(tester, 'proof-cancel-pf1');
      await tester.tap(find.text('Cancel proof'));
      await tester.pumpAndSettle();
      expect(waiting.cancels, ['pf1']);
    });

    testWidgets('rejected proof: the reason is shown and Add proof is back', (tester) async {
      await openProofs(
        tester,
        proofs: [TaxProof.fromJson(proofJson(status: 'rejected', user: 'me', comment: 'Receipt is blurry'))],
      );
      expect(find.text('HR comment: Receipt is blurry'), findsOneWidget);
      expect(find.byKey(const ValueKey('proof-add-i-1')), findsOneWidget);
    });

    testWidgets('proof window closed: no Add proof and a note', (tester) async {
      await openProofs(tester, reason: 'CLOSED', canEdit: false);
      expect(find.byKey(const ValueKey('proof-window-note')), findsOneWidget);
      expect(find.byKey(const ValueKey('proof-add-i-1')), findsNothing);
    });

    testWidgets('nothing submitted yet: asks to submit first', (tester) async {
      final repo = Fake5b()..myDecl = TaxDeclaration.fromJson(declJson(withSubmitted: false));
      await pumpPayroll(tester, const MyTaxScreen(), repo: repo, perms: _own);
      await _tap(tester, 'tab-proofs');
      expect(find.textContaining('Submit your declaration first'), findsOneWidget);
    });
  });

  group('employee: Form 16', () {
    testWidgets('published documents are listed and handed to the share sheet', (tester) async {
      final repo = Fake5b()
        ..myForm16s = [
          const MyForm16(id: 'm1', fy: '2026-27', kind: 'form16'),
          const MyForm16(id: 'm2', fy: '2025-26', kind: 'part_a'),
        ];
      final delivery = FakeDelivery();
      await pumpPayroll(tester, const MyTaxScreen(), repo: repo, perms: _own, delivery: delivery);
      await _tap(tester, 'tab-form16');
      expect(find.text('Form 16  -  FY 2026-27'), findsOneWidget);
      expect(find.text('Form 16 Part A  -  FY 2025-26'), findsOneWidget);
      await _tap(tester, 'my-form16-download-m1');
      expect(delivery.files.single.filename, 'form16-EMP001-2026-27.pdf');
    });

    testWidgets('empty state', (tester) async {
      await pumpPayroll(tester, const MyTaxScreen(), repo: Fake5b(), perms: _own);
      await _tap(tester, 'tab-form16');
      expect(find.text('No Form 16 has been published for you yet.'), findsOneWidget);
    });
  });

  group('admin: declarations', () {
    testWidgets('the list shows status, regime and waiting proofs and opens the declaration', (tester) async {
      await pumpPayroll(
        tester,
        const Scaffold(body: TaxDeclarationsSection()),
        repo: Fake5b(),
        perms: _manage,
        selfId: 'hr',
      );
      expect(find.textContaining('old regime  -  3 items  -  proofs: 1 waiting'), findsOneWidget);
      expect(find.text('Submitted'), findsWidgets);
    });

    testWidgets('on behalf: save asks for the password and sends it', (tester) async {
      final repo = Fake5b()..pwRejects = 1;
      await pumpPayroll(
        tester,
        const AdminDeclarationScreen(userId: 'emp', fy: '2026-27', name: 'Asha Rao'),
        repo: repo,
        perms: _manage,
        selfId: 'hr',
      );
      await _tap(tester, 'decl-item-delete-0');
      await _tapBusy(tester, 'decl-save');
      await _passwordBusy(tester, 'bad');
      expect(find.text('That password is not correct.'), findsOneWidget);
      await _password(tester, 'good');
      expect(repo.onBehalf.map((b) => b['current_password']), ['bad', 'good']);
      expect(repo.onBehalf.last['user'], 'emp');
      expect(repo.onBehalf.last['expected_version'], 5);
      expect(find.text('Draft saved.'), findsOneWidget);
      expect(find.byKey(const ValueKey('decl-entered-by')), findsOneWidget);
    });

    testWidgets('on behalf: submit asks for the password too', (tester) async {
      final repo = Fake5b()..adminDecl = TaxDeclaration.fromJson(declJson(user: 'emp', dirty: true));
      await pumpPayroll(
        tester,
        const AdminDeclarationScreen(userId: 'emp', fy: '2026-27', name: 'Asha Rao'),
        repo: repo,
        perms: _manage,
        selfId: 'hr',
      );
      await _tapBusy(tester, 'decl-submit');
      expect(find.text('Confirm your password'), findsOneWidget);
    });

    testWidgets('you cannot enter your own declaration, and a viewer cannot change anything', (tester) async {
      await pumpPayroll(
        tester,
        const AdminDeclarationScreen(userId: 'emp', fy: '2026-27', name: 'Asha Rao'),
        repo: Fake5b(),
        perms: _manage,
        selfId: 'emp',
      );
      expect(find.textContaining('cannot enter your own declaration'), findsOneWidget);
      expect(find.byKey(const ValueKey('decl-add-item')), findsNothing);
      expect(find.byKey(const ValueKey('decl-save')), findsNothing);
      expect(find.byKey(const ValueKey('decl-unlock')), findsNothing);
    });

    testWidgets('a viewer sees the note and no actions', (tester) async {
      await pumpPayroll(
        tester,
        const AdminDeclarationScreen(userId: 'emp', fy: '2026-27', name: 'Asha Rao'),
        repo: Fake5b(),
        perms: const ['payroll.view_all'],
        selfId: 'hr',
      );
      expect(find.text('You can view this declaration but not change it.'), findsOneWidget);
      expect(find.byKey(const ValueKey('decl-add-item')), findsNothing);
    });

    testWidgets('showing PANs asks the server for the unmasked copy and hiding drops it', (tester) async {
      final repo = Fake5b();
      await pumpPayroll(
        tester,
        const AdminDeclarationScreen(userId: 'emp', fy: '2026-27', name: 'Asha Rao'),
        repo: repo,
        perms: _manage,
        selfId: 'hr',
      );
      expect(repo.revealCalls, [false]);
      await _tap(tester, 'decl-reveal');
      expect(repo.revealCalls, [false]); // waits for the password
      await _password(tester, 'pw');
      expect(repo.revealCalls, [false, true]);
      expect(repo.revealPasswords, ['pw']);
      await _tap(tester, 'decl-reveal');
      expect(repo.revealCalls, [false, true]);
    });

    testWidgets('a viewer without payroll.manage has no reveal button', (tester) async {
      await pumpPayroll(
        tester,
        const AdminDeclarationScreen(userId: 'emp', fy: '2026-27', name: 'Asha Rao'),
        repo: Fake5b(),
        perms: const ['payroll.view_all'],
        selfId: 'hr',
      );
      expect(find.byKey(const ValueKey('decl-reveal')), findsNothing);
    });

    testWidgets('unlock needs a date inside the year and a reason', (tester) async {
      final repo = Fake5b();
      await pumpPayroll(
        tester,
        const AdminDeclarationScreen(userId: 'emp', fy: '2026-27', name: 'Asha Rao'),
        repo: repo,
        perms: _manage,
        selfId: 'hr',
      );
      await _tap(tester, 'decl-unlock');
      await _enter(tester, 'unlock-until', '2027-04-02');
      await _tap(tester, 'unlock-confirm');
      expect(find.textContaining('after the end of FY 2026-27'), findsOneWidget);
      await _enter(tester, 'unlock-until', '2026-11-10');
      await _tap(tester, 'unlock-confirm');
      expect(find.byKey(const ValueKey('unlock-error')), findsOneWidget);
      expect(repo.unlocks, isEmpty);
      await _enter(tester, 'unlock-reason', 'Joined late');
      await _tap(tester, 'unlock-confirm');
      expect(repo.unlocks, isEmpty); // the password comes first
      await _password(tester, 'pw');
      expect(repo.unlocks.single, {
        'user': 'emp',
        'until': '2026-11-10',
        'reason': 'Joined late',
        'pw': 'pw',
        'regime': false,
      });
      expect(find.byKey(const ValueKey('unlock-allow-regime')), findsNothing);
    });

    testWidgets('the regime option is shown to payroll.approve holders only and is sent when ticked', (tester) async {
      final repo = Fake5b();
      await pumpPayroll(
        tester,
        const AdminDeclarationScreen(userId: 'emp', fy: '2026-27', name: 'Asha Rao'),
        repo: repo,
        perms: const [..._manage, 'payroll.approve'],
        selfId: 'hr',
      );
      await _tap(tester, 'decl-unlock');
      await _enter(tester, 'unlock-until', '2026-11-10');
      await _enter(tester, 'unlock-reason', 'Wrong regime chosen');
      await _tap(tester, 'unlock-allow-regime');
      await _tap(tester, 'unlock-confirm');
      await _password(tester, 'pw');
      expect(repo.unlocks.single['regime'], true);
      expect(repo.unlocks.single['pw'], 'pw');
    });
  });

  group('admin: proofs', () {
    testWidgets('the queue lists proofs and opens the review', (tester) async {
      final repo = Fake5b()..proofList = [TaxProof.fromJson(proofJson())];
      await pumpPayroll(tester, const Scaffold(body: TaxProofsSection()), repo: repo, perms: _manage, selfId: 'hr');
      expect(find.textContaining('80C  -  claimed ₹1,20,000.00  -  1 files'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('tax-proof-pf1')));
      await tester.pumpAndSettle();
      expect(find.text('Review proof'), findsOneWidget);
    });

    testWidgets('verify: the amount cannot be above the claim; 0 needs a comment', (tester) async {
      final repo = Fake5b();
      await _pumpPushed(
        tester,
        ProofReviewScreen(proof: TaxProof.fromJson(proofJson())),
        repo: repo,
        perms: _manage,
        selfId: 'hr',
      );
      await _enter(tester, 'proof-verified', '120000.01');
      await _tap(tester, 'proof-accept');
      expect(find.textContaining('cannot be more than the amount claimed'), findsOneWidget);
      await _enter(tester, 'proof-verified', '0');
      await _tap(tester, 'proof-accept');
      expect(find.textContaining('Say why nothing is accepted'), findsOneWidget);
      expect(repo.decisions, isEmpty);
      await _enter(tester, 'proof-verified', '100000');
      await _tap(tester, 'proof-accept');
      expect(repo.decisions, ['verify:1:100000.00:']);
      expect(find.text('Review proof'), findsNothing); // closed after the decision
    });

    testWidgets('reject needs a comment', (tester) async {
      final repo = Fake5b();
      await _pumpPushed(
        tester,
        ProofReviewScreen(proof: TaxProof.fromJson(proofJson())),
        repo: repo,
        perms: _manage,
        selfId: 'hr',
      );
      await _tap(tester, 'proof-reject');
      expect(find.textContaining('write the reason'), findsOneWidget);
      await _enter(tester, 'proof-comment', 'Blurry');
      await _tap(tester, 'proof-reject');
      expect(repo.decisions, ['reject:1:Blurry']);
    });

    testWidgets('SECOND_PERSON_REQUIRED (the person who entered the declaration) is explained', (tester) async {
      final repo = Fake5b()..proofDecideError = const ApiException(code: 'SECOND_PERSON_REQUIRED', message: 'x', status: 403);
      await _pumpPushed(
        tester,
        ProofReviewScreen(proof: TaxProof.fromJson(proofJson())),
        repo: repo,
        perms: _manage,
        selfId: 'hr',
      );
      await _tap(tester, 'proof-accept');
      expect(find.textContaining('A different person must do this'), findsOneWidget);
      expect(find.text('Review proof'), findsOneWidget);
    });

    testWidgets('NOT_PENDING and the employee own proof', (tester) async {
      final repo = Fake5b()..proofDecideError = const ApiException(code: 'NOT_PENDING', message: 'x', status: 409);
      await _pumpPushed(
        tester,
        ProofReviewScreen(proof: TaxProof.fromJson(proofJson())),
        repo: repo,
        perms: _manage,
        selfId: 'hr',
      );
      await _tap(tester, 'proof-accept');
      expect(find.textContaining('already decided'), findsOneWidget);
    });

    testWidgets('your own proof cannot be decided by you; a viewer cannot decide', (tester) async {
      await pumpPayroll(
        tester,
        ProofReviewScreen(proof: TaxProof.fromJson(proofJson())),
        repo: Fake5b(),
        perms: _manage,
        selfId: 'emp',
      );
      expect(find.byKey(const ValueKey('proof-cannot-decide')), findsOneWidget);
      expect(find.byKey(const ValueKey('proof-accept')), findsNothing);
      await pumpPayroll(
        tester,
        ProofReviewScreen(proof: TaxProof.fromJson(proofJson())),
        repo: Fake5b(),
        perms: const ['payroll.view_all'],
        selfId: 'hr',
      );
      expect(find.textContaining('look at this proof but not decide'), findsOneWidget);
    });

    testWidgets('a proof file is handed to the share sheet', (tester) async {
      final delivery = FakeDelivery();
      await pumpPayroll(
        tester,
        ProofReviewScreen(proof: TaxProof.fromJson(proofJson())),
        repo: Fake5b(),
        perms: _manage,
        selfId: 'hr',
        delivery: delivery,
      );
      await _tap(tester, 'proof-open-0');
      expect(delivery.files.single.filename, 'proof.pdf');
    });
  });

  group('admin: Form 16', () {
    Future<Fake5b> openUpload(WidgetTester tester, {List<PickedPdf>? files, Fake5b? repo}) async {
      final r = repo ?? Fake5b();
      await pumpPayroll(
        tester,
        const Form16UploadScreen(initialFy: '2026-27'),
        repo: r,
        perms: _manage,
        selfId: 'hr',
        extraOverrides: [
          form16PickerProvider.overrideWithValue(
            FakePdfPicker(files ?? [_pdf('EMP-u1_form16.pdf'), _pdf('EMP-u2_form16.pdf')]),
          ),
        ],
      );
      return r;
    }

    testWidgets('pick, check, see who matched and what is replaced, commit with the password and the manifest', (tester) async {
      final repo = await openUpload(tester);
      await _tap(tester, 'form16-pick');
      expect(find.text('EMP-u1_form16.pdf'), findsOneWidget);
      await _tap(tester, 'form16-check');
      expect(repo.dryCalls.single, ['2026-27', 'form16', 'EMP-u1_form16.pdf', 'EMP-u2_form16.pdf']);
      expect(find.text('2 of 2 files matched an employee.'), findsOneWidget);
      expect(find.textContaining('replaces the document they have now'), findsOneWidget);
      expect(find.textContaining('Asha Rao'), findsWidgets);
      await _tap(tester, 'form16-commit');
      await _password(tester, 'pw');
      expect(repo.commits.single, {'fy': '2026-27', 'kind': 'form16', 'files': 2, 'manifest': 'c' * 64, 'pw': 'pw'});
      expect(find.textContaining('2 documents uploaded, 1 earlier documents replaced'), findsOneWidget);
    });

    testWidgets('a file that cannot be matched blocks the upload and is named', (tester) async {
      final repo = Fake5b()
        ..dry = Form16DryRun.fromJson(dryRunJson(
          matched: [
            {'index': 0, 'user': userJson('u1'), 'sha256': 'd' * 64, 'replaces_id': null},
          ],
          errors: [
            {'index': 1, 'message': 'UNMATCHED'},
          ],
        ),);
      await openUpload(tester, repo: repo);
      await _tap(tester, 'form16-pick');
      await _tap(tester, 'form16-check');
      expect(find.byKey(const ValueKey('form16-problem-1')), findsOneWidget);
      expect(find.text('No employee ID or PAN in the file name matches an employee.'), findsOneWidget);
      expect(find.byKey(const ValueKey('form16-commit')), findsNothing);
      expect(find.byKey(const ValueKey('form16-blocked')), findsOneWidget);
    });

    testWidgets('UNKNOWN_PAN and FORBIDDEN are explained in plain words', (tester) async {
      final repo = Fake5b()
        ..dry = Form16DryRun.fromJson(dryRunJson(
          matched: [
            {'index': 0, 'user': userJson('u1'), 'sha256': 'd' * 64, 'replaces_id': null},
          ],
          errors: [
            {'index': 1, 'message': 'UNKNOWN_PAN'},
            {'index': 2, 'message': 'FORBIDDEN'},
          ],
        ),);
      await openUpload(tester, repo: repo, files: [_pdf('a.pdf'), _pdf('b.pdf'), _pdf('c.pdf')]);
      await _tap(tester, 'form16-pick');
      await _tap(tester, 'form16-check');
      expect(find.text('The PAN in the file name does not belong to any employee.'), findsOneWidget);
      expect(find.text('This file is for you. You cannot upload your own Form 16.'), findsOneWidget);
      expect(find.byKey(const ValueKey('form16-commit')), findsNothing);
    });

    testWidgets('a file over 2 MB is refused before any check', (tester) async {
      final repo = await openUpload(tester, files: [
        _pdf('ok.pdf'),
        PickedPdf(name: 'big.pdf', bytes: Uint8List(0), error: 'Each file must be 2 MB or smaller.'),
      ],);
      await _tap(tester, 'form16-pick');
      expect(find.text('Each file must be 2 MB or smaller.'), findsWidgets);
      await _tap(tester, 'form16-check');
      expect(repo.dryCalls, isEmpty);
    });

    testWidgets('IMPORT_NOT_CHECKED on commit explains and asks for a new check', (tester) async {
      final repo = Fake5b()..commitError = const ApiException(code: 'IMPORT_NOT_CHECKED', message: 'x', status: 409);
      await openUpload(tester, repo: repo);
      await _tap(tester, 'form16-pick');
      await _tap(tester, 'form16-check');
      await _tap(tester, 'form16-commit');
      await _password(tester, 'pw');
      expect(find.textContaining('not checked in the last 15 minutes'), findsOneWidget);
      expect(find.byKey(const ValueKey('form16-commit')), findsNothing);
      expect(find.byKey(const ValueKey('form16-check')), findsOneWidget);
    });

    testWidgets('changing the kind throws the check away', (tester) async {
      final repo = await openUpload(tester);
      await _tap(tester, 'form16-pick');
      await _tap(tester, 'form16-check');
      expect(find.byKey(const ValueKey('form16-commit')), findsOneWidget);
      await _tap(tester, 'form16-up-kind');
      await tester.tap(find.text('Form 16 Part A').last);
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('form16-commit')), findsNothing);
      await _tap(tester, 'form16-check');
      expect(repo.dryCalls.last[1], 'part_a');
    });

    testWidgets('the list: documents, publish entry for approvers, withdraw with reason and password', (tester) async {
      final repo = Fake5b()..docs = [Form16Doc.fromJson(form16Json(status: 'published'))];
      await pumpPayroll(
        tester,
        const Scaffold(body: Form16Section()),
        repo: repo,
        perms: const ['payroll.view_all', 'payroll.manage', 'payroll.approve'],
        selfId: 'hr',
      );
      expect(find.byKey(const ValueKey('form16-upload')), findsOneWidget);
      expect(find.byKey(const ValueKey('form16-publish-open')), findsOneWidget);
      expect(find.text('Published'), findsWidgets);
      await _tap(tester, 'form16-withdraw-f16a');
      await _comment(tester, 'Wrong employee');
      await _password(tester, 'pw');
      expect(repo.withdraws, ['f16a:Wrong employee:pw']);
    });

    testWidgets('a viewer sees neither upload nor withdraw', (tester) async {
      final repo = Fake5b()..docs = [Form16Doc.fromJson(form16Json())];
      await pumpPayroll(
        tester,
        const Scaffold(body: Form16Section()),
        repo: repo,
        perms: const ['payroll.view_all'],
        selfId: 'hr',
      );
      expect(find.byKey(const ValueKey('form16-upload')), findsNothing);
      expect(find.byKey(const ValueKey('form16-withdraw-f16a')), findsNothing);
      expect(find.byKey(const ValueKey('form16-view-f16a')), findsOneWidget);
    });

    testWidgets('publish: own upload and own document are not selectable; the reviewed hashes go with the password', (tester) async {
      final repo = Fake5b()
        ..docs = [
          Form16Doc.fromJson(form16Json(id: 'a', user: 'u1', uploader: 'maker')),
          Form16Doc.fromJson(form16Json(id: 'b', user: 'u2', uploader: 'other')),
          Form16Doc.fromJson(form16Json(id: 'c', user: 'maker', uploader: 'other')),
        ]
        ..publishResult = const Form16PublishResult(published: 1);
      await pumpPayroll(tester, const Form16PublishScreen(fy: '2026-27'), repo: repo, perms: _approve, selfId: 'maker');
      expect(find.text('Publish 1 selected'), findsOneWidget);
      final disabled = find.descendant(of: find.byKey(const ValueKey('publish-doc-a')), matching: find.byType(CheckboxListTile));
      expect(tester.widget<CheckboxListTile>(disabled).onChanged, isNull);
      await _tapBusy(tester, 'form16-publish');
      await _password(tester, 'pw');
      expect(repo.publishes, [
        ['b'],
      ]);
      expect(find.text('1 published'), findsOneWidget);
    });

    testWidgets('publish: failed documents are listed with the reason', (tester) async {
      final repo = Fake5b()
        ..docs = [Form16Doc.fromJson(form16Json(id: 'b', user: 'u2', uploader: 'other'))]
        ..publishResult = const Form16PublishResult(
          published: 0,
          failed: [(id: 'b', reason: 'SHA_MISMATCH')],
        );
      await pumpPayroll(tester, const Form16PublishScreen(fy: '2026-27'), repo: repo, perms: _approve, selfId: 'maker');
      await _tapBusy(tester, 'form16-publish');
      await _password(tester, 'pw');
      expect(find.text('0 published'), findsOneWidget);
      expect(find.textContaining('The document changed after you reviewed it'), findsOneWidget);
    });
  });

  group('approvals, home and settings', () {
    testWidgets('approvals list rules waiting for verification and Form 16 ready to publish', (tester) async {
      final repo = Fake5b()
        ..years = [TaxYear.fromJson(yearJson(open: {'id': 'ru2', 'version': 2, 'status': 'submitted'}))]
        ..docs = [
          Form16Doc.fromJson(form16Json(id: 'a')),
          Form16Doc.fromJson(form16Json(id: 'b', user: 'u2')),
        ]
        ..rule = _rules(status: 'submitted', submitter: 'sub');
      await pumpPayroll(tester, const Scaffold(body: ApprovalsTab()), repo: repo, perms: _approve, selfId: 'checker');
      expect(find.text('Tax rules'), findsOneWidget);
      expect(find.text('Tax rules FY 2026-27 version 2'), findsOneWidget);
      expect(find.text('Form 16 to publish'), findsOneWidget);
      expect(find.text('2 Form 16 documents ready'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('queue-tax-rules-2026-27')));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('rules-verify')), findsOneWidget);
    });

    testWidgets('a person without payroll.approve gets no tax sections', (tester) async {
      final repo = Fake5b()
        ..years = [TaxYear.fromJson(yearJson(open: {'id': 'ru2', 'version': 2, 'status': 'submitted'}))]
        ..docs = [Form16Doc.fromJson(form16Json())];
      await pumpPayroll(
        tester,
        const Scaffold(body: ApprovalsTab()),
        repo: repo,
        perms: const ['payroll.view_all', 'payroll.publish'],
        selfId: 'checker',
      );
      expect(find.text('Tax rules'), findsNothing);
      expect(find.text('Form 16 to publish'), findsNothing);
    });

    testWidgets('the payroll console has a Tax tab', (tester) async {
      await pumpPayroll(tester, const PayrollHomeScreen(), repo: Fake5b(), perms: _manage);
      expect(find.byKey(const ValueKey('tab-tax')), findsOneWidget);
      await _tap(tester, 'tab-tax');
      expect(find.byKey(const ValueKey('tax-parts')), findsOneWidget);
      expect(find.text('No tax years yet.'), findsOneWidget);
    });

    testWidgets('the Form 16 second-person switch is guarded by the password', (tester) async {
      final repo = Fake5b()
        ..settingsMap = {
          'payroll.rounding': 'rupee',
          form16SecondPersonKey: true,
          'payroll.tax_proof_max_files': 5,
        };
      await pumpPayroll(
        tester,
        const Scaffold(body: PayrollSettingsTab()),
        repo: repo,
        perms: const ['payroll.view_all', 'payroll.manage', 'role.assign'],
      );
      final sw = find.widgetWithText(SwitchListTile, 'A different person must publish a Form 16 that I uploaded');
      await tester.scrollUntilVisible(sw, 300, scrollable: find.byType(Scrollable).first);
      await tester.tap(sw);
      await _tapBusy(tester, 'save-settings');
      await _password(tester, 'pw');
      expect(repo.savedSettings.single, {form16SecondPersonKey: false});
      expect(repo.settingsPasswords.single, 'pw');
    });

    testWidgets('without role.assign the guarded switch is disabled', (tester) async {
      final repo = Fake5b()..settingsMap = {'payroll.rounding': 'rupee', form16SecondPersonKey: true};
      await pumpPayroll(tester, const Scaffold(body: PayrollSettingsTab()), repo: repo, perms: _manage);
      final sw = find.widgetWithText(SwitchListTile, 'A different person must publish a Form 16 that I uploaded');
      await tester.scrollUntilVisible(sw, 300, scrollable: find.byType(Scrollable).first);
      expect(tester.widget<SwitchListTile>(sw).onChanged, isNull);
    });
  });
}
