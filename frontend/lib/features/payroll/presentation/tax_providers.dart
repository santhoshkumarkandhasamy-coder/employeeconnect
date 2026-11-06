import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../admin/presentation/admin_providers.dart' show AdminPagedNotifier;
import '../../announcements/presentation/announcement_providers.dart' show PagedList;
import '../../attendance/domain/attendance_models.dart' show Paged;
import '../data/payroll_repository.dart';
import '../domain/phase5b_models.dart';

/// Tax state (contract 19.6 to 19.8). Everything is autoDispose and in memory only: declarations hold
/// third-party identifiers and are never written to the JSON cache or to disk.

final taxYearsProvider = FutureProvider.autoDispose<List<TaxYear>>(
  (ref) => ref.watch(payrollRepositoryProvider).taxYears(),
);

final taxRuleVersionsProvider = FutureProvider.autoDispose.family<List<TaxRules>, String>(
  (ref, fy) => ref.watch(payrollRepositoryProvider).taxRuleVersions(fy),
);

final taxRulesProvider = FutureProvider.autoDispose.family<TaxRules, String>(
  (ref, id) => ref.watch(payrollRepositoryProvider).taxRules(id),
);

/// Sections of the VERIFIED rules of a year, for the admin on-behalf form. Falls back to the built-in list
/// when the rules cannot be read or none is verified.
final verifiedSectionsProvider = FutureProvider.autoDispose.family<List<SectionDef>, String>((ref, fy) async {
  try {
    final versions = await ref.watch(taxRuleVersionsProvider(fy).future);
    for (final v in versions) {
      if (v.isVerified && v.sections.isNotEmpty) return v.sections;
    }
  } on Object {
    // fall through
  }
  return fallbackSections;
});

class TaxDeclarationsNotifier extends AdminPagedNotifier<DeclarationListItem, TaxDeclarationQuery> {
  @override
  Future<Paged<DeclarationListItem>> fetchPage(TaxDeclarationQuery arg, int page) =>
      ref.read(payrollRepositoryProvider).taxDeclarations(arg, page: page);

  @override
  String idOf(DeclarationListItem item) => '${item.user.id}-${item.fy}';
}

final taxDeclarationsProvider = AsyncNotifierProvider.autoDispose
    .family<TaxDeclarationsNotifier, PagedList<DeclarationListItem>, TaxDeclarationQuery>(
  TaxDeclarationsNotifier.new,
);

/// Family argument: (user id, fy). The revealed copy is never cached here: the screen fetches it directly.
typedef DeclarationKey = (String userId, String fy);

final adminDeclarationProvider = FutureProvider.autoDispose.family<TaxDeclaration, DeclarationKey>(
  (ref, key) => ref.watch(payrollRepositoryProvider).taxDeclarationOf(key.$1, fy: key.$2),
);

class TaxProofsNotifier extends AdminPagedNotifier<TaxProof, TaxProofQuery> {
  @override
  Future<Paged<TaxProof>> fetchPage(TaxProofQuery arg, int page) =>
      ref.read(payrollRepositoryProvider).taxProofs(arg, page: page);

  @override
  String idOf(TaxProof item) => item.id;
}

final taxProofsProvider = AsyncNotifierProvider.autoDispose
    .family<TaxProofsNotifier, PagedList<TaxProof>, TaxProofQuery>(TaxProofsNotifier.new);

class Form16Notifier extends AdminPagedNotifier<Form16Doc, Form16Query> {
  @override
  Future<Paged<Form16Doc>> fetchPage(Form16Query arg, int page) =>
      ref.read(payrollRepositoryProvider).form16Documents(arg, page: page);

  @override
  String idOf(Form16Doc item) => item.id;
}

final form16DocsProvider = AsyncNotifierProvider.autoDispose
    .family<Form16Notifier, PagedList<Form16Doc>, Form16Query>(Form16Notifier.new);

// ---- employee ------------------------------------------------------------------------------------

final myTaxYearsProvider = FutureProvider.autoDispose<List<MyTaxYear>>(
  (ref) => ref.watch(payrollRepositoryProvider).myTaxYears(),
);

final myDeclarationProvider = FutureProvider.autoDispose.family<TaxDeclaration, String>(
  (ref, fy) => ref.watch(payrollRepositoryProvider).myDeclaration(fy: fy),
);

final myProofsProvider = FutureProvider.autoDispose.family<List<TaxProof>, String>(
  (ref, fy) => ref.watch(payrollRepositoryProvider).myProofs(fy: fy),
);

final myForm16Provider = FutureProvider.autoDispose<List<MyForm16>>(
  (ref) => ref.watch(payrollRepositoryProvider).myForm16(),
);

/// Work waiting in the Approvals queue (counts for the badge and the sections).
class TaxApprovals {
  const TaxApprovals({this.rules = const [], this.form16 = const []});

  /// Years whose open version is waiting for verification.
  final List<TaxYear> rules;

  /// Uploaded Form 16 documents that nobody has published yet.
  final List<Form16Doc> form16;

  int get count => rules.length + form16.length;
}
