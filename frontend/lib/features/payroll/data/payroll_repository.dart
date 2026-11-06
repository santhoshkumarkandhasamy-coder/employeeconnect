import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/api/dio_client.dart';
import '../../attendance/domain/attendance_models.dart' show Paged;
import '../../export/data/export_repository.dart' show decodeErrorBody, decodeStreamErrorBody;
import '../../../core/files/file_service.dart' show fileIdFromUrl;
import '../domain/claims_models.dart';
import '../domain/pay_items_models.dart';
import '../domain/phase4a_models.dart';
import '../domain/phase4b_models.dart';
import '../domain/phase5a_models.dart';
import '../domain/phase5b_models.dart';
import '../domain/payroll_models.dart';

/// A downloaded file held in memory (a payslip PDF or the register workbook).
class PayrollFile {
  const PayrollFile(this.bytes, this.filename);
  final Uint8List bytes;
  final String filename;
}

/// Largest PDF / workbook accepted from the server.
const int payrollMaxFileBytes = 25 * 1024 * 1024;

/// File name from `Content-Disposition`, reduced to `[A-Za-z0-9_.-]` and forced
/// to end in [ext] (never trust a server path).
String safePayrollFileName(String? disposition, String fallback, String ext) {
  if (disposition == null) return fallback;
  final m = RegExp('filename="?([^";]+)"?').firstMatch(disposition);
  var n = m?[1] ?? '';
  n = n.split(RegExp(r'[\\/]')).last.replaceAll(RegExp(r'[^A-Za-z0-9_.-]'), '_');
  if (n.isEmpty || n.startsWith('.')) return fallback;
  return n.toLowerCase().endsWith(ext) ? n : '$n$ext';
}

/// api-contract.md section 13. Nothing is cached, persisted or logged here.
class PayrollRepository {
  PayrollRepository(this._dio);
  final Dio _dio;

  static const _a = '/admin/payroll';

  Future<Map<String, dynamic>> _json(
    String method,
    String path, {
    Object? body,
    Map<String, Object?>? query,
  }) =>
      guardApi<Map<String, dynamic>>(() async {
        final res = await _dio.request<Object?>(
          path,
          data: body,
          queryParameters: query,
          options: Options(method: method),
        );
        final d = res.data;
        return d is Map ? d.cast<String, dynamic>() : const <String, dynamic>{};
      });

  Future<PayrollFile> _file(
    String method,
    String path, {
    required String fallbackName,
    required String ext,
    Object? body,
  }) async {
    try {
      final res = await _dio.request<List<int>>(
        path,
        data: body,
        options: Options(method: method, responseType: ResponseType.bytes),
      );
      final data = res.data ?? const <int>[];
      if (data.length > payrollMaxFileBytes) {
        throw const ApiException(code: 'FILE_TOO_LARGE', message: 'The file is too large.');
      }
      return PayrollFile(
        Uint8List.fromList(data),
        safePayrollFileName(res.headers.value('content-disposition'), fallbackName, ext),
      );
    } on DioException catch (e) {
      throw ApiException.fromDio(decodeErrorBody(e));
    }
  }

  // ---- employee ------------------------------------------------------------------

  Future<List<PayslipListItem>> myPayslips(int year) async =>
      PayslipListItem.parseList(await _json('GET', '/payslips', query: {'year': year}));

  Future<Payslip> myPayslip(String id) async =>
      Payslip.fromJson(await _json('GET', '/payslips/$id'));

  Future<PayrollFile> myPayslipPdf(String id) =>
      _file('GET', '/payslips/$id/pdf', fallbackName: 'payslip.pdf', ext: '.pdf');

  // ---- settings ------------------------------------------------------------------

  Future<Map<String, Object?>> settings() async {
    final j = await _json('GET', '$_a/settings');
    final s = j['settings'];
    return s is Map ? s.cast<String, Object?>() : <String, Object?>{};
  }

  /// [currentPassword] (the caller's own) is required by the server only when the
  /// change flips `payroll.publish_requires_second_person` (contract 13.3).
  Future<Map<String, Object?>> saveSettings(
    Map<String, Object?> changes, {
    String? currentPassword,
  }) async {
    final j = await _json('PUT', '$_a/settings', body: {
      'settings': changes,
      if (currentPassword != null) 'current_password': currentPassword,
    },);
    final s = j['settings'];
    return s is Map ? s.cast<String, Object?>() : <String, Object?>{};
  }

  // ---- employees, structures, details --------------------------------------------

  Future<Paged<PayrollEmployee>> employees(PayrollEmployeeQuery q, {int page = 1}) async =>
      Paged.parse(
        await _json('GET', '$_a/employees', query: {
          if (q.q.trim().isNotEmpty) 'q': q.q.trim(),
          if (q.hasStructure != null) 'has_structure': q.hasStructure,
          'page': page,
          'page_size': 20,
        },),
        PayrollEmployee.fromJson,
      );

  Future<List<SalaryStructure>> structures(String userId) async =>
      SalaryStructure.parseList(await _json('GET', '$_a/employees/$userId/structures'));

  Future<SalaryStructure> createStructure(String userId, StructureInput input) async =>
      SalaryStructure.fromJson(
        await _json('POST', '$_a/employees/$userId/structures', body: input.toJson()),
      );

  Future<SalaryStructure> updateStructure(String id, StructureInput input) async =>
      SalaryStructure.fromJson(await _json('PUT', '$_a/structures/$id', body: input.toJson()));

  Future<void> deleteStructure(String id) async {
    await _json('DELETE', '$_a/structures/$id');
  }

  Future<PayrollDetails> details(String userId, {bool reveal = false}) async =>
      PayrollDetails.fromJson(
        await _json('GET', '$_a/employees/$userId/details', query: {'reveal': reveal}),
      );

  /// [currentPassword] is the caller's own password (step-up). Never stored.
  Future<PayrollDetails> saveDetails(
    String userId,
    Map<String, Object?> changes, {
    required String currentPassword,
  }) async =>
      PayrollDetails.fromJson(
        await _json('PUT', '$_a/employees/$userId/details',
            body: {...changes, 'current_password': currentPassword},),
      );

  // ---- runs ----------------------------------------------------------------------

  Future<List<PayrollRunItem>> runs(int year) async =>
      PayrollRunItem.parseList(await _json('GET', '$_a/runs', query: {'year': year}));

  Future<PayrollRun> createRun(String period, {String? payDate}) async => PayrollRun.fromJson(
        await _json('POST', '$_a/runs', body: {'period': period, 'pay_date': payDate}),
      );

  Future<PayrollRun> run(String id) async => PayrollRun.fromJson(await _json('GET', '$_a/runs/$id'));

  Future<void> deleteRun(String id) async {
    await _json('DELETE', '$_a/runs/$id');
  }

  Future<PayrollRun> refreshRun(String id) async =>
      PayrollRun.fromJson(await _json('POST', '$_a/runs/$id/refresh'));

  Future<PayrollRun> processRun(String id) async =>
      PayrollRun.fromJson(await _json('POST', '$_a/runs/$id/process'));

  Future<PayrollRun> reopenRun(String id) async =>
      PayrollRun.fromJson(await _json('POST', '$_a/runs/$id/reopen'));

  Future<PayrollRun> publishRun(String id, {required String currentPassword}) async =>
      PayrollRun.fromJson(
        await _json('POST', '$_a/runs/$id/publish', body: {'current_password': currentPassword}),
      );

  Future<Paged<RunPayslipRow>> runPayslips(RunPayslipQuery q, {int page = 1}) async =>
      Paged.parse(
        await _json('GET', '$_a/runs/${q.runId}/payslips', query: {
          if (q.q.trim().isNotEmpty) 'q': q.q.trim(),
          if (q.flag != null) 'flag': q.flag,
          'page': page,
          'page_size': 20,
        },),
        RunPayslipRow.fromJson,
      );

  Future<Payslip> revise(String runId, String userId, String reason) async => Payslip.fromJson(
        await _json('POST', '$_a/runs/$runId/employees/$userId/revise', body: {'reason': reason}),
      );

  /// [currentPassword] is required (and needs `payroll.manage`) when
  /// [includeBank] is true.
  Future<PayrollFile> register(
    String runId,
    String period, {
    bool includeBank = false,
    String? currentPassword,
  }) =>
      _file(
        'POST',
        '$_a/runs/$runId/register',
        fallbackName: 'payroll-register-$period.xlsx',
        ext: '.xlsx',
        body: {
          'include_bank': includeBank,
          if (includeBank && currentPassword != null) 'current_password': currentPassword,
        },
      );

  // ---- dashboard and reports (plan 14 phase 1) ---------------------------------------

  Future<Map<String, dynamic>> dashboard(String period) =>
      _json('GET', '$_a/dashboard', query: {'period': period});

  Future<Map<String, dynamic>> variance(String period) =>
      _json('GET', '$_a/variance', query: {'period': period});

  /// [kind]: department | headcount | ctc | lop | bank_advice. `bank_advice`
  /// needs `payroll.manage` and [currentPassword].
  Future<PayrollFile> report(String kind, String period, {String? currentPassword}) => _file(
        'POST',
        '$_a/reports/$kind?period=$period',
        fallbackName: 'payroll-$kind-$period.xlsx',
        ext: '.xlsx',
        body: {if (currentPassword != null) 'current_password': currentPassword},
      );

  // ---- statutory (plan 14 phase 2, contract section 16) ---------------------------

  static const _st = '$_a/statutory';

  Future<List<StatutoryMonth>> statutoryDues(String period, {int months = 1}) async =>
      StatutoryMonth.parseList(
        await _json('GET', '$_st/dues', query: {'period': period, 'months': months}),
      );

  /// [kind]: pf_ecr (text) | esi | pt (workbooks). Needs `payroll.view_all`.
  /// `pf_ecr` and `esi` contain UAN / ESI IP numbers and need [currentPassword]
  /// (step-up, `REAUTH_FAILED` when wrong); `pt` does not.
  /// 409 STATUTORY_BLOCKERS / RUN_NOT_PUBLISHED surface as [ApiException].
  Future<PayrollFile> statutoryFile(String kind, String period, {String? currentPassword}) => _file(
        'POST',
        '$_st/$kind?period=$period',
        fallbackName: kind == 'pf_ecr' ? 'pf-ecr-$period.txt' : 'statutory-$kind-$period.xlsx',
        ext: kind == 'pf_ecr' ? '.txt' : '.xlsx',
        body: {if (currentPassword != null) 'current_password': currentPassword},
      );

  /// Marks a challan paid / unpaid. [currentPassword] is always required.
  Future<void> setChallan({
    required String type,
    required String period,
    required bool paid,
    required String currentPassword,
    String? paidOn,
    String? reference,
    String? amount,
  }) async {
    await _json('PUT', '$_st/challans', body: {
      'type': type,
      'period': period,
      'status': paid ? 'paid' : 'unpaid',
      if (paid) 'paid_on': paidOn,
      if (paid) 'reference': reference,
      if (paid && amount != null && amount.isNotEmpty) 'amount': amount,
      'current_password': currentPassword,
    },);
  }

  // ---- phase 4a: CTC, gratuity, increments, bulk hike, structure import (section 18) --------

  /// Pure calculation; nothing is stored. Needs `payroll.view_all`.
  Future<CtcResult> ctcCalculate(CtcInput input) async =>
      CtcResult.fromJson(await _json('POST', '$_a/ctc/calculate', body: input.toJson()));

  /// [GratuityQuery.asOf] is `YYYY-MM-DD` (null = today). Totals cover the whole filtered set.
  Future<GratuityPage> gratuity(GratuityQuery q, {int page = 1}) async => GratuityPage.fromJson(
        await _json('GET', '$_a/gratuity', query: {
          if (q.asOf != null) 'as_of': q.asOf,
          if (q.departmentId != null) 'department_id': q.departmentId,
          'eligible_only': q.eligibleOnly,
          if (q.q.trim().isNotEmpty) 'q': q.q.trim(),
          'page': page,
          'page_size': 50,
        },),
      );

  /// Gratuity workbook for a month (as of its last day). Needs `payroll.view_all`.
  Future<PayrollFile> gratuityReport(String period) => report('gratuity', period);

  Future<Paged<Increment>> increments(IncrementQuery q, {int page = 1}) async => Paged.parse(
        await _json('GET', '$_a/increments', query: {
          if (q.status != null) 'status': q.status,
          if (q.userId != null) 'user_id': q.userId,
          if (q.batchId != null) 'batch_id': q.batchId,
          if (q.effectiveFrom != null) 'effective_from': q.effectiveFrom,
          'page': page,
          'page_size': 20,
        },),
        Increment.fromJson,
      );

  Future<Increment> increment(String id) async =>
      Increment.fromJson(await _json('GET', '$_a/increments/$id'));

  Future<Increment> createIncrement(String userId, IncrementInput input) async => Increment.fromJson(
        await _json('POST', '$_a/employees/$userId/increments', body: input.toJson()),
      );

  Future<Increment> updateIncrement(String id, IncrementInput input, {required int expectedVersion}) async =>
      Increment.fromJson(
        await _json('PUT', '$_a/increments/$id', body: input.toJson(expectedVersion: expectedVersion)),
      );

  Future<Increment> submitIncrement(String id, {required int expectedVersion}) async => Increment.fromJson(
        await _json('POST', '$_a/increments/$id/submit', body: {'expected_version': expectedVersion}),
      );

  Future<Increment> withdrawIncrement(String id, {required int expectedVersion}) async => Increment.fromJson(
        await _json('POST', '$_a/increments/$id/withdraw', body: {'expected_version': expectedVersion}),
      );

  /// Needs `payroll.approve` and the caller's own password (step-up, never kept).
  Future<Increment> approveIncrement(
    String id, {
    required int expectedVersion,
    required String currentPassword,
    String? comment,
  }) async =>
      Increment.fromJson(await _json('POST', '$_a/increments/$id/approve', body: {
        'expected_version': expectedVersion,
        'comment': (comment == null || comment.trim().isEmpty) ? null : comment.trim(),
        'current_password': currentPassword,
      },),);

  Future<Increment> rejectIncrement(String id, {required int expectedVersion, required String comment}) async =>
      Increment.fromJson(await _json('POST', '$_a/increments/$id/reject', body: {
        'expected_version': expectedVersion,
        'comment': comment.trim(),
      },),);

  Future<Increment> cancelIncrement(String id, String reason) async => Increment.fromJson(
        await _json('POST', '$_a/increments/$id/cancel', body: {'reason': reason.trim()}),
      );

  Future<List<SalaryHistoryEntry>> salaryHistory(String userId) async =>
      SalaryHistoryEntry.parseList(await _json('GET', '$_a/employees/$userId/salary-history'));

  Future<BulkHikeResult> bulkHike(BulkHikeInput input) async =>
      BulkHikeResult.fromJson(await _json('POST', '$_a/increments/bulk', body: input.toJson()));

  /// [kind] `percent` | `import` | null (all).
  Future<Paged<IncrementBatch>> incrementBatches({String? kind, int page = 1}) async => Paged.parse(
        await _json('GET', '$_a/increments/batches', query: {
          if (kind != null) 'kind': kind,
          'page': page,
          'page_size': 20,
        },),
        IncrementBatch.fromJson,
      );

  Future<IncrementBatch> incrementBatch(String id) async =>
      IncrementBatch.fromJson(await _json('GET', '$_a/increments/batches/$id'));

  Future<BatchSubmitResult> submitIncrementBatch(String id) async =>
      BatchSubmitResult.fromJson(await _json('POST', '$_a/increments/batches/$id/submit', body: const {}));

  /// Id and version of every submitted row of a batch (what a checker reviews).
  Future<List<({String id, int version})>> submittedBatchRows(String batchId) async {
    final out = <({String id, int version})>[];
    for (var page = 1; page <= 30; page++) {
      final r = await increments(IncrementQuery(status: 'submitted', batchId: batchId), page: page);
      for (final i in r.items) {
        out.add((id: i.id, version: i.version));
      }
      if (!r.hasMore) break;
    }
    return out;
  }

  /// [expected] = the rows and versions the checker saw; the server skips any other.
  Future<BatchApproveResult> approveIncrementBatch(
    String id, {
    required String currentPassword,
    required List<({String id, int version})> expected,
    String? comment,
  }) async =>
      BatchApproveResult.fromJson(await _json('POST', '$_a/increments/batches/$id/approve', body: {
        'expected': [
          for (final e in expected) {'id': e.id, 'version': e.version},
        ],
        'comment': (comment == null || comment.trim().isEmpty) ? null : comment.trim(),
        'current_password': currentPassword,
      },),);

  /// Returns the number of rows rejected.
  Future<int> rejectIncrementBatch(String id, {required String comment}) async {
    final j = await _json('POST', '$_a/increments/batches/$id/reject', body: {'comment': comment.trim()});
    final n = j['rejected'];
    return n is num ? n.toInt() : 0;
  }

  Future<BatchCancelResult> cancelIncrementBatch(String id, String reason) async =>
      BatchCancelResult.fromJson(
        await _json('POST', '$_a/increments/batches/$id/cancel', body: {'reason': reason.trim()}),
      );

  Future<PayrollFile> structuresTemplate() => _file(
        'GET',
        '$_a/structures/template',
        fallbackName: 'salary-structures-template.xlsx',
        ext: '.xlsx',
      );

  Future<StructureImportReport> structureImportDryRun({
    required List<int> bytes,
    required String filename,
  }) async =>
      StructureImportReport.fromJson(await _json(
        'POST',
        '$_a/structures/import',
        query: {'dry_run': true},
        body: FormData.fromMap({'file': MultipartFile.fromBytes(bytes, filename: filename)}),
      ),);

  /// Re-sends the same file with the dry run's [fileSha256], a [reason] (3-200)
  /// and the caller's own password (step-up, never kept).
  Future<StructureImportCommit> structureImportCommit({
    required List<int> bytes,
    required String filename,
    required String fileSha256,
    required String reason,
    required String currentPassword,
  }) async =>
      StructureImportCommit.fromJson(await _json(
        'POST',
        '$_a/structures/import',
        query: {'dry_run': false},
        body: FormData.fromMap({
          'file': MultipartFile.fromBytes(bytes, filename: filename),
          'file_sha256': fileSha256,
          'reason': reason.trim(),
          'current_password': currentPassword,
        }),
      ),);

  // ---- pay items: one-time items and bulk import (contract 17.2) ---------------------

  Future<Paged<OneTimeItem>> oneTimeItems(ItemQuery q, {int page = 1}) async => Paged.parse(
        await _json('GET', '$_a/items', query: {
          'period': q.period,
          if (q.userId != null) 'user_id': q.userId,
          if (q.code != null) 'code': q.code,
          if (q.status != null) 'status': q.status,
          if (q.batchId != null) 'batch_id': q.batchId,
          'page': page,
          'page_size': 20,
        },),
        OneTimeItem.fromJson,
      );

  Future<OneTimeItem> createItem({
    required String userId,
    required String period,
    required String code,
    required String label,
    required String amount,
  }) async =>
      OneTimeItem.fromJson(await _json('POST', '$_a/items', body: {
        'user_id': userId,
        'period': period,
        'code': code,
        'label': label,
        'amount': amount,
      },),);

  /// Only the given keys are sent (the server needs at least one).
  Future<OneTimeItem> updateItem(
    String id, {
    required int expectedVersion,
    String? period,
    String? label,
    String? amount,
  }) async =>
      OneTimeItem.fromJson(await _json('PUT', '$_a/items/$id', body: {
        'expected_version': expectedVersion,
        if (period != null) 'period': period,
        if (label != null) 'label': label,
        if (amount != null) 'amount': amount,
      },),);

  Future<OneTimeItem> cancelItem(String id, String reason) async =>
      OneTimeItem.fromJson(await _json('POST', '$_a/items/$id/cancel', body: {'reason': reason}));

  Future<PayrollFile> itemsTemplate() => _file(
        'GET',
        '$_a/items/template',
        fallbackName: 'payroll-items-template.xlsx',
        ext: '.xlsx',
      );

  FormData _importForm(List<int> bytes, String filename, String period, Map<String, String> extra) =>
      FormData.fromMap({
        'file': MultipartFile.fromBytes(bytes, filename: filename),
        'period': period,
        ...extra,
      });

  /// Dry run: nothing is written. Needs `payroll.manage`.
  Future<ImportReport> importDryRun({
    required List<int> bytes,
    required String filename,
    required String period,
  }) async =>
      ImportReport.fromJson(await _json(
        'POST',
        '$_a/items/import',
        query: {'dry_run': true},
        body: _importForm(bytes, filename, period, const {}),
      ),);

  /// Commit: re-sends the same file with the dry run's [fileSha256] and the
  /// caller's own password (step-up). The password is never kept.
  Future<ImportCommit> importCommit({
    required List<int> bytes,
    required String filename,
    required String period,
    required String fileSha256,
    required String currentPassword,
  }) async =>
      ImportCommit.fromJson(await _json(
        'POST',
        '$_a/items/import',
        query: {'dry_run': false},
        body: _importForm(bytes, filename, period, {
          'file_sha256': fileSha256,
          'current_password': currentPassword,
        },),
      ),);

  Future<List<ImportBatch>> importBatches({String? period}) async => ImportBatch.parseList(
        await _json('GET', '$_a/items/batches', query: {if (period != null) 'period': period}),
      );

  Future<BatchCancelResult> cancelBatch(String id, String reason) async =>
      BatchCancelResult.fromJson(
        await _json('POST', '$_a/items/batches/$id/cancel', body: {'reason': reason}),
      );

  // ---- loans (17.3) ------------------------------------------------------------------

  Future<Paged<Loan>> loans(LoanQuery q, {int page = 1}) async => Paged.parse(
        await _json('GET', '$_a/loans', query: {
          if (q.status != null) 'status': q.status,
          if (q.kind != null) 'kind': q.kind,
          'page': page,
          'page_size': 20,
        },),
        Loan.fromJson,
      );

  Future<Loan> loan(String id) async => Loan.fromJson(await _json('GET', '$_a/loans/$id'));

  Future<Loan> createLoan({
    required String userId,
    required String kind,
    required String principal,
    required String emi,
    required String startPeriod,
    required String reason,
    required String currentPassword,
  }) async =>
      Loan.fromJson(await _json('POST', '$_a/loans', body: {
        'user_id': userId,
        'kind': kind,
        'principal': principal,
        'emi': emi,
        'start_period': startPeriod,
        'reason': reason,
        'current_password': currentPassword,
      },),);

  /// No step-up for skip (contract 17.3 note 11).
  Future<Loan> skipInstallment(
    String loanId, {
    required String installmentId,
    required String reason,
    required int expectedVersion,
  }) async =>
      Loan.fromJson(await _json('POST', '$_a/loans/$loanId/skip', body: {
        'installment_id': installmentId,
        'reason': reason,
        'expected_version': expectedVersion,
      },),);

  Future<Loan> rescheduleLoan(
    String loanId, {
    required String emi,
    required String fromPeriod,
    required String reason,
    required int expectedVersion,
    required String currentPassword,
  }) async =>
      Loan.fromJson(await _json('POST', '$_a/loans/$loanId/reschedule', body: {
        'emi': emi,
        'from_period': fromPeriod,
        'reason': reason,
        'expected_version': expectedVersion,
        'current_password': currentPassword,
      },),);

  /// [mode] `payroll` needs [period]; `outside` needs [paidOn] (YYYY-MM-DD)
  /// and [reference].
  Future<Loan> forecloseLoan(
    String loanId, {
    required String mode,
    required String reason,
    required int expectedVersion,
    required String currentPassword,
    String? period,
    String? paidOn,
    String? reference,
  }) async =>
      Loan.fromJson(await _json('POST', '$_a/loans/$loanId/foreclose', body: {
        'mode': mode,
        if (mode == 'payroll') 'period': period,
        if (mode == 'outside') ...{'paid_on': paidOn, 'reference': reference},
        'reason': reason,
        'expected_version': expectedVersion,
        'current_password': currentPassword,
      },),);

  Future<Loan> cancelLoan(
    String loanId, {
    required String reason,
    required int expectedVersion,
    required String currentPassword,
  }) async =>
      Loan.fromJson(await _json('POST', '$_a/loans/$loanId/cancel', body: {
        'reason': reason,
        'expected_version': expectedVersion,
        'current_password': currentPassword,
      },),);

  // ---- employee loans (read-only) ----------------------------------------------------

  Future<List<MyLoan>> myLoans() async => MyLoan.parseList(await _json('GET', '/me/loans'));

  Future<MyLoan> myLoan(String id) async => MyLoan.fromJson(await _json('GET', '/me/loans/$id'));

  // ---- arrears (17.4) ----------------------------------------------------------------

  Future<Paged<Arrears>> arrears(ArrearsQuery q, {int page = 1}) async => Paged.parse(
        await _json('GET', '$_a/arrears', query: {
          if (q.status != null) 'status': q.status,
          'page': page,
          'page_size': 20,
        },),
        Arrears.fromJson,
      );

  Future<Arrears> arrearsDetail(String id) async =>
      Arrears.fromJson(await _json('GET', '$_a/arrears/$id'));

  /// Null = no difference (an open proposal is then dismissed by the system).
  Future<Arrears?> recalculateArrears(String userId) async {
    final j = await _json('POST', '$_a/employees/$userId/arrears/recalculate');
    final a = j['arrears'];
    return a is Map ? Arrears.fromJson(a.cast<String, dynamic>()) : null;
  }

  Future<Arrears> acceptArrears(
    String id, {
    required String period,
    required int expectedVersion,
    required String currentPassword,
  }) async =>
      Arrears.fromJson(await _json('POST', '$_a/arrears/$id/accept', body: {
        'period': period,
        'expected_version': expectedVersion,
        'current_password': currentPassword,
      },),);

  Future<Arrears> dismissArrears(String id, String reason) async =>
      Arrears.fromJson(await _json('POST', '$_a/arrears/$id/dismiss', body: {'reason': reason}));

  // ---- leave encashment (17.5) -------------------------------------------------------

  Future<Paged<Encashment>> encashments(EncashmentQuery q, {int page = 1}) async => Paged.parse(
        await _json('GET', '$_a/encashments', query: {
          'year': q.year,
          if (q.status != null) 'status': q.status,
          'page': page,
          'page_size': 20,
        },),
        Encashment.fromJson,
      );

  Future<EncashmentQuote> encashmentQuote(
    String userId, {
    required String leaveTypeId,
    required int year,
    required String period,
    double? days,
  }) async =>
      EncashmentQuote.fromJson(await _json(
        'GET',
        '$_a/employees/$userId/encashment-quote',
        query: {
          'leave_type_id': leaveTypeId,
          'year': year,
          'period': period,
          if (days != null) 'days': days,
        },
      ),);

  Future<Encashment> createEncashment({
    required String userId,
    required String leaveTypeId,
    required int year,
    required double days,
    required String period,
    required String reason,
    required String currentPassword,
  }) async =>
      Encashment.fromJson(await _json('POST', '$_a/encashments', body: {
        'user_id': userId,
        'leave_type_id': leaveTypeId,
        'year': year,
        'days': days,
        'period': period,
        'source': 'request',
        'reason': reason,
        'current_password': currentPassword,
      },),);

  Future<Encashment> cancelEncashment(String id, {String? reason}) async =>
      Encashment.fromJson(await _json('POST', '$_a/encashments/$id/cancel',
          body: reason == null ? null : {'reason': reason},),);

  /// Active leave types that can be encashed (`GET /admin/leave-types` is open
  /// to every signed-in user; filtered here on `encashable`).
  Future<List<LeaveTypeRef>> encashableLeaveTypes() async {
    final j = await _json('GET', '/admin/leave-types', query: {'is_active': true, 'page_size': 100});
    final items = j['items'];
    return [
      if (items is List)
        for (final e in items)
          if (e is Map && e['encashable'] == true) LeaveTypeRef.fromJson(e),
    ];
  }

  // ---- full and final settlement, admin (18.8) ------------------------------------

  Future<Paged<FnfListItem>> fnfList(FnfQuery q, {int page = 1}) async => Paged.parse(
        await _json('GET', '$_a/fnf', query: {
          if (q.status != null) 'status': q.status,
          if (q.q.trim().isNotEmpty) 'q': q.q.trim(),
          'page': page,
          'page_size': 20,
        },),
        FnfListItem.fromJson,
      );

  Future<Fnf> fnf(String id) async => Fnf.fromJson(await _json('GET', '$_a/fnf/$id'));

  Future<Fnf> createFnf(String userId, Map<String, Object?> body) async =>
      Fnf.fromJson(await _json('POST', '$_a/employees/$userId/fnf', body: body));

  Future<Fnf> updateFnf(String id, FnfEdit edit, {required int expectedVersion}) async =>
      Fnf.fromJson(await _json('PUT', '$_a/fnf/$id', body: edit.toJson(expectedVersion)));

  Future<FnfPreview> fnfPreview(String id) async =>
      FnfPreview.fromJson(await _json('GET', '$_a/fnf/$id/preview'));

  Future<Fnf> submitFnf(String id, {required int expectedVersion}) async => Fnf.fromJson(
        await _json('POST', '$_a/fnf/$id/submit', body: {'expected_version': expectedVersion}),
      );

  Future<Fnf> withdrawFnf(String id, {required int expectedVersion}) async => Fnf.fromJson(
        await _json('POST', '$_a/fnf/$id/withdraw', body: {'expected_version': expectedVersion}),
      );

  /// Needs `payroll.approve` and the caller's own password (step-up, never kept).
  Future<Fnf> approveFnf(
    String id, {
    required int expectedVersion,
    required String currentPassword,
    String? comment,
  }) async =>
      Fnf.fromJson(await _json('POST', '$_a/fnf/$id/approve', body: {
        'expected_version': expectedVersion,
        'comment': (comment == null || comment.trim().isEmpty) ? null : comment.trim(),
        'current_password': currentPassword,
      },),);

  Future<Fnf> rejectFnf(String id, {required int expectedVersion, required String comment}) async =>
      Fnf.fromJson(await _json('POST', '$_a/fnf/$id/reject', body: {
        'expected_version': expectedVersion,
        'comment': comment.trim(),
      },),);

  /// Needs `payroll.approve` and the caller's own password (step-up, never kept).
  Future<Fnf> revokeFnf(
    String id, {
    required int expectedVersion,
    required String reason,
    required String currentPassword,
  }) async =>
      Fnf.fromJson(await _json('POST', '$_a/fnf/$id/revoke', body: {
        'expected_version': expectedVersion,
        'reason': reason.trim(),
        'current_password': currentPassword,
      },),);

  Future<Fnf> cancelFnf(String id, String reason) async =>
      Fnf.fromJson(await _json('POST', '$_a/fnf/$id/cancel', body: {'reason': reason.trim()}));

  Future<PayrollFile> fnfStatement(String id) =>
      _file('GET', '$_a/fnf/$id/statement', fallbackName: 'fnf-statement.pdf', ext: '.pdf');

  /// Notice period of the employee's payroll details, sent alone (no password,
  /// contract 13.4). Null = the company default.
  Future<PayrollDetails> saveNoticePeriod(String userId, {required int? days}) async =>
      PayrollDetails.fromJson(
        await _json('PUT', '$_a/employees/$userId/details', body: {'notice_period_days': days}),
      );

  // ---- full and final settlement, employee (18.9) -----------------------------------

  Future<MyFnf?> myFnf() async => MyFnf.parse(await _json('GET', '/me/fnf'));

  Future<PayrollFile> myFnfStatement() =>
      _file('GET', '/me/fnf/statement', fallbackName: 'fnf-statement.pdf', ext: '.pdf');

  // ---- admin payslips -------------------------------------------------------------

  Future<Payslip> payslip(String id) async =>
      Payslip.fromJson(await _json('GET', '$_a/payslips/$id'));

  Future<Payslip> savePayslip(String id, Map<String, Object?> patch) async =>
      Payslip.fromJson(await _json('PUT', '$_a/payslips/$id', body: patch));

  Future<void> deletePayslip(String id) async {
    await _json('DELETE', '$_a/payslips/$id');
  }

  Future<Payslip> processPayslip(String id) async =>
      Payslip.fromJson(await _json('POST', '$_a/payslips/$id/process'));

  Future<Payslip> reopenPayslip(String id) async =>
      Payslip.fromJson(await _json('POST', '$_a/payslips/$id/reopen'));

  Future<Payslip> publishPayslip(String id, {required String currentPassword}) async =>
      Payslip.fromJson(
        await _json('POST', '$_a/payslips/$id/publish', body: {'current_password': currentPassword}),
      );

  Future<PayrollFile> payslipPdf(String id) =>
      _file('GET', '$_a/payslips/$id/pdf', fallbackName: 'payslip.pdf', ext: '.pdf');

  /// Employee payroll details: ONLY `overtime_eligible`, sent alone. The server
  /// asks for no password in that case (contract 17.1); `null` is refused (422).
  Future<PayrollDetails> saveOvertimeEligible(String userId, {required bool eligible}) async =>
      PayrollDetails.fromJson(
        await _json('PUT', '$_a/employees/$userId/details', body: {'overtime_eligible': eligible}),
      );

  // ---- reimbursement claims, employee (17.6) ------------------------------------------

  Future<ClaimCategories> claimCategories() async =>
      ClaimCategories.fromJson(await _json('GET', '/reimbursements/categories'));

  /// `POST /files` with `purpose=reimbursement_bill`; returns the file id.
  /// The caller has already checked the size and type ([validateClaimBill]).
  Future<String> uploadBill(List<int> bytes, String filename) => guardApi<String>(() async {
        final form = FormData.fromMap({
          'purpose': 'reimbursement_bill',
          'file': MultipartFile.fromBytes(
            bytes,
            filename: filename,
            contentType: DioMediaType.parse(billContentTypeFor(filename)),
          ),
        });
        final res = await _dio.post<Map<String, dynamic>>('/files', data: form);
        return '${res.data?['id'] ?? ''}';
      });

  Future<Claim> submitClaim({
    required String category,
    required String expenseDate,
    required String amount,
    required String description,
    String? billFileId,
  }) async =>
      Claim.fromJson(await _json('POST', '/reimbursements', body: {
        'category': category,
        'expense_date': expenseDate,
        'amount': amount,
        'description': description,
        'bill_file_id': billFileId,
      },),);

  Future<Paged<Claim>> myClaims(ClaimQuery q, {int page = 1}) async => Paged.parse(
        await _json('GET', '/reimbursements', query: {
          if (q.status != null) 'status': q.status,
          if (q.year != null) 'year': q.year,
          'page': page,
          'page_size': 20,
        },),
        Claim.fromJson,
      );

  Future<Claim> myClaim(String id) async => Claim.fromJson(await _json('GET', '/reimbursements/$id'));

  Future<Claim> cancelClaim(String id) async =>
      Claim.fromJson(await _json('POST', '/reimbursements/$id/cancel'));

  /// A bill held in memory, fetched through the app client (bearer header,
  /// pinning). The link from the server is reduced to its file id, so a
  /// tampered link cannot send the token elsewhere.
  Future<Uint8List> billBytes(String url) async {
    final id = fileIdFromUrl(url);
    if (id == null) throw const ApiException(code: 'NOT_FOUND', message: 'The bill is not available.');
    try {
      final res = await _dio.get<ResponseBody>(
        '/files/$id',
        options: Options(responseType: ResponseType.stream),
      );
      const tooLarge = ApiException(code: 'FILE_TOO_LARGE', message: 'The file is too large.');
      final declared = int.tryParse(res.headers.value(Headers.contentLengthHeader) ?? '');
      if (declared != null && declared > payrollMaxFileBytes) {
        await res.data?.stream.listen(null).cancel();
        throw tooLarge;
      }
      final builder = BytesBuilder(copy: false);
      await for (final chunk in res.data!.stream) {
        builder.add(chunk);
        if (builder.length > payrollMaxFileBytes) throw tooLarge; // never buffer more than the cap
      }
      return builder.takeBytes();
    } on DioException catch (e) {
      throw ApiException.fromDio(await decodeStreamErrorBody(e));
    }
  }

  // ---- reimbursement claims, manager and HR (17.6) --------------------------------------

  Future<Paged<Claim>> adminClaims(AdminClaimQuery q, {int page = 1}) async => Paged.parse(
        await _json('GET', '/admin/reimbursements', query: {
          'stage': q.stage,
          if (q.status != null) 'status': q.status,
          if (q.period != null) 'period': q.period,
          if (q.category != null) 'category': q.category,
          if (q.employeeId != null && q.employeeId!.trim().isNotEmpty) 'employee_id': q.employeeId!.trim(),
          'page': page,
          'page_size': 20,
        },),
        Claim.fromJson,
      );

  Future<Claim> adminClaim(String id) async =>
      Claim.fromJson(await _json('GET', '/admin/reimbursements/$id'));

  Future<Claim> verifyClaim(String id, {String? comment}) async => Claim.fromJson(await _json(
        'PUT',
        '/admin/reimbursements/$id/verify',
        body: {if (comment != null && comment.trim().isNotEmpty) 'comment': comment.trim()},
      ),);

  Future<Claim> approveClaim(
    String id, {
    required String approvedAmount,
    required String period,
    required int expectedVersion,
    String? comment,
  }) async =>
      Claim.fromJson(await _json('PUT', '/admin/reimbursements/$id/approve', body: {
        'approved_amount': approvedAmount,
        'period': period,
        if (comment != null && comment.trim().isNotEmpty) 'comment': comment.trim(),
        'expected_version': expectedVersion,
      },),);

  Future<Claim> rejectClaim(String id, {required String comment}) async => Claim.fromJson(
        await _json('PUT', '/admin/reimbursements/$id/reject', body: {'comment': comment.trim()}),
      );

  Future<Claim> payoutClaim(String id, {required String period, required int expectedVersion}) async =>
      Claim.fromJson(await _json('PUT', '/admin/reimbursements/$id/payout', body: {
        'period': period,
        'expected_version': expectedVersion,
      },),);

  // ---- overtime (17.8) ----------------------------------------------------------------------

  Future<OvertimeComputeResult> computeOvertime(String period) async => OvertimeComputeResult.fromJson(
        await _json('POST', '$_a/overtime/compute', query: {'period': period}),
      );

  Map<String, Object?> _overtimeQuery(OvertimeQuery q, int page, int pageSize) => {
        'period': q.period,
        if (q.status != null) 'status': q.status,
        if (q.userId != null) 'user_id': q.userId,
        'page': page,
        'page_size': pageSize,
      };

  Future<Paged<OvertimeDay>> overtimeDays(OvertimeQuery q, {int page = 1}) async => Paged.parse(
        await _json('GET', '$_a/overtime', query: _overtimeQuery(q, page, 50)),
        OvertimeDay.fromJson,
      );

  /// Per-employee totals of the filtered month (no money). Read from a page of one row.
  Future<List<OvertimeSummary>> overtimeSummary(OvertimeQuery q) async =>
      OvertimeSummary.parseList(await _json('GET', '$_a/overtime', query: _overtimeQuery(q, 1, 1)));

  Future<OvertimeDay> approveOvertime(
    String id, {
    required int approvedMinutes,
    required int expectedVersion,
    String? comment,
  }) async =>
      OvertimeDay.fromJson(await _json('PUT', '$_a/overtime/$id', body: {
        'status': 'approved',
        'approved_minutes': approvedMinutes,
        'comment': (comment == null || comment.trim().isEmpty) ? null : comment.trim(),
        'expected_version': expectedVersion,
      },),);

  Future<OvertimeDay> rejectOvertime(String id, {required String comment, required int expectedVersion}) async =>
      OvertimeDay.fromJson(await _json('PUT', '$_a/overtime/$id', body: {
        'status': 'rejected',
        'comment': comment.trim(),
        'expected_version': expectedVersion,
      },),);

  /// [action] is `approve` or `reject` (comment required for reject); 1..200 ids.
  Future<OvertimeBulkResult> bulkOvertime(
    List<String> ids, {
    required String action,
    String? comment,
    bool includeFlagged = false,
  }) async =>
      OvertimeBulkResult.fromJson(await _json('POST', '$_a/overtime/bulk', body: {
        'ids': ids,
        'action': action,
        if (includeFlagged) 'include_flagged': true,
        if (comment != null && comment.trim().isNotEmpty) 'comment': comment.trim(),
      },),);

  Future<MyOvertime> myOvertime(String period) async =>
      MyOvertime.fromJson(await _json('GET', '/me/overtime', query: {'period': period}));

  // ---- phase 5a: publish requests (contract 19.2) -------------------------------------

  Future<PublishCheck> publishCheckRun(String runId) async =>
      PublishCheck.fromJson(await _json('GET', '$_a/runs/$runId/publish-check'));

  Future<PublishCheck> publishCheckPayslip(String payslipId) async =>
      PublishCheck.fromJson(await _json('GET', '$_a/payslips/$payslipId/publish-check'));

  Future<PublishRequest> createRunPublishRequest(String runId, {String? comment}) async =>
      PublishRequest.fromJson(await _json('POST', '$_a/runs/$runId/publish-requests', body: {
        'comment': _blankToNull(comment),
      },),);

  Future<PublishRequest> createPayslipPublishRequest(String payslipId, {String? comment}) async =>
      PublishRequest.fromJson(await _json('POST', '$_a/payslips/$payslipId/publish-requests', body: {
        'comment': _blankToNull(comment),
      },),);

  Future<Paged<PublishRequest>> publishRequests(PublishRequestQuery q, {int page = 1}) async =>
      Paged.parse(
        await _json('GET', '$_a/publish-requests', query: {
          if (q.status != null) 'status': q.status,
          if (q.runId != null) 'run_id': q.runId,
          'page': page,
          'page_size': 20,
        },),
        PublishRequest.fromJson,
      );

  Future<PublishRequest> publishRequest(String id) async =>
      PublishRequest.fromJson(await _json('GET', '$_a/publish-requests/$id'));

  Future<PublishRequest> withdrawPublishRequest(String id, {required int expectedVersion}) async =>
      PublishRequest.fromJson(await _json('POST', '$_a/publish-requests/$id/withdraw', body: {
        'expected_version': expectedVersion,
      },),);

  /// Needs `payroll.publish` and the caller's own password (step-up, never kept).
  Future<PublishApproveResult> approvePublishRequest(
    String id, {
    required int expectedVersion,
    required String currentPassword,
    String? comment,
  }) async =>
      PublishApproveResult.fromJson(await _json('POST', '$_a/publish-requests/$id/approve', body: {
        'expected_version': expectedVersion,
        'comment': _blankToNull(comment),
        'current_password': currentPassword,
      },),);

  Future<PublishRequest> rejectPublishRequest(
    String id, {
    required int expectedVersion,
    required String comment,
  }) async =>
      PublishRequest.fromJson(await _json('POST', '$_a/publish-requests/$id/reject', body: {
        'expected_version': expectedVersion,
        'comment': comment.trim(),
      },),);

  // ---- phase 5a: delivery (19.3) ---------------------------------------------------------

  Future<DeliveryPage> delivery(DeliveryQuery q, {int page = 1}) async =>
      DeliveryPage.fromJson(await _json('GET', '$_a/runs/${q.runId}/delivery', query: {
        if (q.state != null) 'state': q.state,
        if (q.q.trim().isNotEmpty) 'q': q.q.trim(),
        'page': page,
        'page_size': 50,
      },),);

  /// [userIds] null = every employee who has not opened the payslip and has a phone.
  Future<RemindResult> remindDelivery(String runId, {List<String>? userIds}) async =>
      RemindResult.fromJson(await _json('POST', '$_a/runs/$runId/delivery/remind', body: {
        'user_ids': userIds,
      },),);

  // ---- phase 5a: company bank accounts (19.4) --------------------------------------------

  Future<List<BankAccount>> bankAccounts() => guardApi<List<BankAccount>>(() async {
        final res = await _dio.request<Object?>('$_a/bank-accounts', options: Options(method: 'GET'));
        final d = res.data;
        if (d is List) {
          return [
            for (final e in d)
              if (e is Map) BankAccount.fromJson(e.cast<String, dynamic>()),
          ];
        }
        return d is Map ? BankAccount.parseList(d.cast<String, dynamic>()) : <BankAccount>[];
      });

  /// Needs `payroll.manage` and the caller's own password (step-up, never kept).
  Future<BankAccount> createBankAccount(BankAccountInput input, {required String currentPassword}) async =>
      BankAccount.fromJson(await _json('POST', '$_a/bank-accounts', body: input.toJson(currentPassword)));

  /// Only the provided fields are sent; the account number and IFSC cannot change.
  /// Reactivating an account and changing the default flag need [currentPassword] (step-up, never kept).
  Future<BankAccount> updateBankAccount(
    String id, {
    String? label,
    bool? isDefault,
    bool? active,
    String? currentPassword,
  }) async =>
      BankAccount.fromJson(await _json('PUT', '$_a/bank-accounts/$id', body: {
        if (label != null) 'label': label.trim(),
        if (isDefault != null) 'is_default': isDefault,
        if (active != null) 'active': active,
        if (currentPassword != null) 'current_password': currentPassword,
      },),);

  // ---- phase 5a: payment batches (19.5) ----------------------------------------------------

  Future<PaymentBatch> createPaymentBatch(String runId, PaymentBatchCreate body) async =>
      PaymentBatch.fromJson(await _json('POST', '$_a/runs/$runId/payment-batches', body: body.toJson()));

  Future<Paged<PaymentBatch>> paymentBatches(PaymentBatchQuery q, {int page = 1}) async => Paged.parse(
        await _json('GET', '$_a/payment-batches', query: {
          if (q.status != null) 'status': q.status,
          if (q.runId != null) 'run_id': q.runId,
          'page': page,
          'page_size': 20,
        },),
        PaymentBatch.fromJson,
      );

  Future<PaymentBatch> paymentBatch(String id) async =>
      PaymentBatch.fromJson(await _json('GET', '$_a/payment-batches/$id'));

  /// An empty [edit] refreshes the draft's stale lines.
  Future<PaymentBatch> updatePaymentBatch(String id, PaymentBatchEdit edit, {required int expectedVersion}) async =>
      PaymentBatch.fromJson(await _json('PUT', '$_a/payment-batches/$id', body: edit.toJson(expectedVersion)));

  Future<PaymentBatch> submitPaymentBatch(String id, {required int expectedVersion}) async =>
      PaymentBatch.fromJson(await _json('POST', '$_a/payment-batches/$id/submit', body: {
        'expected_version': expectedVersion,
      },),);

  Future<PaymentBatch> withdrawPaymentBatch(String id, {required int expectedVersion}) async =>
      PaymentBatch.fromJson(await _json('POST', '$_a/payment-batches/$id/withdraw', body: {
        'expected_version': expectedVersion,
      },),);

  /// Needs `payroll.approve` and the caller's own password (step-up, never kept).
  Future<PaymentBatch> approvePaymentBatch(
    String id, {
    required int expectedVersion,
    required String currentPassword,
    String? comment,
  }) async =>
      PaymentBatch.fromJson(await _json('POST', '$_a/payment-batches/$id/approve', body: {
        'expected_version': expectedVersion,
        'comment': _blankToNull(comment),
        'current_password': currentPassword,
      },),);

  Future<PaymentBatch> rejectPaymentBatch(
    String id, {
    required int expectedVersion,
    required String comment,
  }) async =>
      PaymentBatch.fromJson(await _json('POST', '$_a/payment-batches/$id/reject', body: {
        'expected_version': expectedVersion,
        'comment': comment.trim(),
      },),);

  Future<PaymentBatch> cancelPaymentBatch(String id, String reason) async =>
      PaymentBatch.fromJson(await _json('POST', '$_a/payment-batches/$id/cancel', body: {
        'reason': reason.trim(),
      },),);

  /// The approved CSV with the unmasked accounts. The first call marks the batch
  /// exported; later calls return the same bytes. Needs the caller's password.
  /// Held in memory only and handed to the share sheet by the screen.
  Future<PayrollFile> paymentFile(String id, {required String currentPassword}) => _file(
        'POST',
        '$_a/payment-batches/$id/file',
        fallbackName: 'bank-payment.csv',
        ext: '.csv',
        body: {'current_password': currentPassword},
      );

  Future<PaymentBatch> confirmPayment(String id, PaymentConfirm body, {required String currentPassword}) async =>
      PaymentBatch.fromJson(
        await _json('POST', '$_a/payment-batches/$id/confirm', body: body.toJson(currentPassword)),
      );

  // ---- phase 5b: tax year rules (19.6) -------------------------------------------------------

  static const _t = '/admin/payroll/tax';

  Future<List<TaxYear>> taxYears() async => TaxYear.parseList(await _json('GET', '$_t/years'));

  /// All versions of a financial year, newest first.
  Future<List<TaxRules>> taxRuleVersions(String fy) async =>
      TaxRules.parseList(await _json('GET', '$_t/years/$fy/rules'));

  Future<TaxRules> taxRules(String id) async => TaxRules.fromJson(await _json('GET', '$_t/rules/$id'));

  /// A new draft = a copy of the newest version (409 DRAFT_EXISTS when one is open).
  Future<TaxRules> createTaxRules(String fy) async =>
      TaxRules.fromJson(await _json('POST', '$_t/years/$fy/rules', body: <String, Object?>{}));

  /// The whole `rules` object is replaced. [note]: null keeps it, empty removes it.
  Future<TaxRules> saveTaxRules(
    String id, {
    required int versionRow,
    required Map<String, dynamic> rules,
    String? note,
  }) async =>
      TaxRules.fromJson(await _json('PUT', '$_t/rules/$id', body: {
        'version_row': versionRow,
        'rules': rules,
        if (note != null) 'note': note.trim(),
      },),);

  Future<TaxRules> submitTaxRules(String id, {required int versionRow}) async =>
      TaxRules.fromJson(await _json('POST', '$_t/rules/$id/submit', body: {'version_row': versionRow}));

  Future<TaxRules> withdrawTaxRules(String id, {required int versionRow}) async =>
      TaxRules.fromJson(await _json('POST', '$_t/rules/$id/withdraw', body: {'version_row': versionRow}));

  /// Needs `payroll.approve` and the caller's own password (step-up, never kept).
  Future<TaxRules> verifyTaxRules(
    String id, {
    required int versionRow,
    required String rulesSha256,
    required String attestation,
    required String currentPassword,
  }) async =>
      TaxRules.fromJson(await _json('POST', '$_t/rules/$id/verify', body: {
        'version_row': versionRow,
        'rules_sha256': rulesSha256,
        'attestation': attestation.trim(),
        'current_password': currentPassword,
      },),);

  Future<TaxRules> rejectTaxRules(String id, {required int versionRow, required String comment}) async =>
      TaxRules.fromJson(await _json('POST', '$_t/rules/$id/reject', body: {
        'version_row': versionRow,
        'comment': comment.trim(),
      },),);

  Future<TaxWindows> saveTaxWindows(String fy, TaxWindows w) async =>
      TaxWindows.fromJson((await _json('PUT', '$_t/years/$fy/windows', body: w.toJson()))['windows']);

  // ---- phase 5b: declarations and proofs, admin (19.7) ---------------------------------------

  Future<Paged<DeclarationListItem>> taxDeclarations(TaxDeclarationQuery q, {int page = 1}) async => Paged.parse(
        await _json('GET', '$_t/declarations', query: {
          if (q.fy != null) 'fy': q.fy,
          if (q.status != null) 'status': q.status,
          if (q.regime != null) 'regime': q.regime,
          if (q.q.trim().isNotEmpty) 'q': q.q.trim(),
          'page': page,
          'page_size': 20,
        },),
        DeclarationListItem.fromJson,
      );

  /// [reveal] shows the landlord / lender PAN unmasked (audited, rate limited). Never kept.
  Future<TaxDeclaration> taxDeclarationOf(String userId, {String? fy, bool reveal = false}) async =>
      TaxDeclaration.fromJson(await _json('GET', '$_t/declarations/$userId', query: {
        if (fy != null) 'fy': fy,
        'reveal': reveal,
      },),);

  /// On behalf of an employee: needs `payroll.manage` and the caller's password.
  Future<TaxDeclaration> saveDeclarationOnBehalf(
    String userId,
    DeclarationSave body, {
    required String currentPassword,
  }) async =>
      TaxDeclaration.fromJson(await _json('PUT', '$_t/declarations/$userId', body: body.toJson(currentPassword)));

  Future<TaxDeclaration> submitDeclarationOnBehalf(
    String userId, {
    required String fy,
    required int expectedVersion,
    required String currentPassword,
  }) async =>
      TaxDeclaration.fromJson(await _json('POST', '$_t/declarations/$userId/submit', body: {
        'fy': fy,
        'expected_version': expectedVersion,
        'current_password': currentPassword,
      },),);

  Future<TaxDeclaration> unlockDeclaration(
    String userId, {
    required String fy,
    required String until,
    required String reason,
  }) async =>
      TaxDeclaration.fromJson(await _json('POST', '$_t/declarations/$userId/unlock', body: {
        'fy': fy,
        'until': until,
        'reason': reason.trim(),
      },),);

  Future<Paged<TaxProof>> taxProofs(TaxProofQuery q, {int page = 1}) async => Paged.parse(
        await _json('GET', '$_t/proofs', query: {
          if (q.fy != null) 'fy': q.fy,
          if (q.status != null) 'status': q.status,
          if (q.userId != null) 'user_id': q.userId,
          'page': page,
          'page_size': 20,
        },),
        TaxProof.fromJson,
      );

  Future<TaxProof> verifyTaxProof(
    String id, {
    required int expectedVersion,
    required String verified,
    String? comment,
  }) async =>
      TaxProof.fromJson(await _json('POST', '$_t/proofs/$id/verify', body: {
        'expected_version': expectedVersion,
        'verified': verified,
        'comment': _blankToNull(comment),
      },),);

  Future<TaxProof> rejectTaxProof(String id, {required int expectedVersion, required String comment}) async =>
      TaxProof.fromJson(await _json('POST', '$_t/proofs/$id/reject', body: {
        'expected_version': expectedVersion,
        'comment': comment.trim(),
      },),);

  /// A proof file, fetched through the app client. The link from the server is reduced to its file id.
  Future<PayrollFile> taxProofFile(ProofFile f) async {
    final id = fileIdFromUrl(f.url);
    if (id == null) throw const ApiException(code: 'NOT_FOUND', message: 'The file is not available.');
    return _file('GET', '/files/$id', fallbackName: 'proof${f.extension}', ext: f.extension);
  }

  // ---- phase 5b: Form 16, admin (19.8) ----------------------------------------------------------

  /// [dryRun] true checks and matches only. The commit (false) also needs the manifest of the dry run
  /// and the caller's password. File names are sent as chosen: the server matches on them and never keeps them.
  Future<Map<String, dynamic>> form16Upload({
    required String fy,
    required String kind,
    required List<Form16UploadFile> files,
    required bool dryRun,
    String? manifestSha256,
    String? currentPassword,
  }) =>
      guardApi<Map<String, dynamic>>(() async {
        final form = FormData();
        for (final f in files) {
          form.files.add(MapEntry(
            'files',
            MultipartFile.fromBytes(f.bytes, filename: f.name, contentType: DioMediaType.parse('application/pdf')),
          ),);
        }
        if (!dryRun) {
          form.fields
            ..add(MapEntry('manifest_sha256', manifestSha256 ?? ''))
            ..add(MapEntry('current_password', currentPassword ?? ''));
        }
        final res = await _dio.post<Object?>(
          '$_t/form16/upload',
          data: form,
          queryParameters: {'fy': fy, 'kind': kind, 'dry_run': dryRun},
        );
        final d = res.data;
        return d is Map ? d.cast<String, dynamic>() : const <String, dynamic>{};
      });

  Future<Form16DryRun> form16DryRun(String fy, String kind, List<Form16UploadFile> files) async =>
      Form16DryRun.fromJson(await form16Upload(fy: fy, kind: kind, files: files, dryRun: true));

  Future<Form16CommitResult> form16Commit(
    String fy,
    String kind,
    List<Form16UploadFile> files, {
    required String manifestSha256,
    required String currentPassword,
  }) async =>
      Form16CommitResult.fromJson(await form16Upload(
        fy: fy,
        kind: kind,
        files: files,
        dryRun: false,
        manifestSha256: manifestSha256,
        currentPassword: currentPassword,
      ),);

  Future<Paged<Form16Doc>> form16Documents(Form16Query q, {int page = 1}) async => Paged.parse(
        await _json('GET', '$_t/form16', query: {
          if (q.fy != null) 'fy': q.fy,
          if (q.kind != null) 'kind': q.kind,
          if (q.status != null) 'status': q.status,
          if (q.q.trim().isNotEmpty) 'q': q.q.trim(),
          'page': page,
          'page_size': 20,
        },),
        Form16Doc.fromJson,
      );

  Future<PayrollFile> form16AdminFile(String id) =>
      _file('GET', '$_t/form16/$id/file', fallbackName: 'form16.pdf', ext: '.pdf');

  /// Needs `payroll.approve` and the caller's password. [documents] are the ids and hashes that were reviewed.
  Future<Form16PublishResult> publishForm16(
    String fy,
    List<({String id, String sha256})> documents, {
    required String currentPassword,
  }) async =>
      Form16PublishResult.fromJson(await _json('POST', '$_t/form16/publish', body: {
        'fy': fy,
        'documents': [for (final d in documents) {'id': d.id, 'sha256': d.sha256}],
        'current_password': currentPassword,
      },),);

  Future<void> withdrawForm16(String id, {required String reason, required String currentPassword}) async {
    await _json('POST', '$_t/form16/$id/withdraw', body: {
      'reason': reason.trim(),
      'current_password': currentPassword,
    },);
  }

  // ---- phase 5b: employee self-service (19.7, 19.8) ---------------------------------------------

  Future<List<MyTaxYear>> myTaxYears() async => MyTaxYear.parseList(await _json('GET', '/me/tax/years'));

  Future<TaxDeclaration> myDeclaration({String? fy}) async =>
      TaxDeclaration.fromJson(await _json('GET', '/me/tax/declaration', query: {if (fy != null) 'fy': fy}));

  Future<TaxDeclaration> saveMyDeclaration(DeclarationSave body) async =>
      TaxDeclaration.fromJson(await _json('PUT', '/me/tax/declaration', body: body.toJson(null)));

  Future<TaxDeclaration> submitMyDeclaration({required String fy, required int expectedVersion}) async =>
      TaxDeclaration.fromJson(await _json('POST', '/me/tax/declaration/submit', body: {
        'fy': fy,
        'expected_version': expectedVersion,
      },),);

  /// `POST /files` with `purpose=tax_proof`; returns the file id. The caller has checked type and size.
  Future<String> uploadTaxProofFile(List<int> bytes, String filename) => guardApi<String>(() async {
        final form = FormData.fromMap({
          'purpose': 'tax_proof',
          'file': MultipartFile.fromBytes(
            bytes,
            filename: filename,
            contentType: DioMediaType.parse(billContentTypeFor(filename)),
          ),
        });
        final res = await _dio.post<Map<String, dynamic>>('/files', data: form);
        return '${res.data?['id'] ?? ''}';
      });

  Future<TaxProof> createMyProof({
    required String fy,
    required String itemId,
    required String claimed,
    required List<String> fileIds,
  }) async =>
      TaxProof.fromJson(await _json('POST', '/me/tax/proofs', body: {
        'fy': fy,
        'item_id': itemId,
        'claimed': claimed,
        'file_ids': fileIds,
      },),);

  Future<List<TaxProof>> myProofs({String? fy}) async =>
      TaxProof.parseList(await _json('GET', '/me/tax/proofs', query: {if (fy != null) 'fy': fy}));

  Future<TaxProof> cancelMyProof(String id) async =>
      TaxProof.fromJson(await _json('POST', '/me/tax/proofs/$id/cancel', body: <String, Object?>{}));

  Future<List<MyForm16>> myForm16() async => MyForm16.parseList(await _json('GET', '/me/tax/form16'));

  Future<PayrollFile> myForm16File(String id) =>
      _file('GET', '/me/tax/form16/$id/file', fallbackName: 'form16.pdf', ext: '.pdf');
}

/// One PDF of a Form 16 upload, in memory.
class Form16UploadFile {
  const Form16UploadFile(this.name, this.bytes);
  final String name;
  final Uint8List bytes;
}

String? _blankToNull(String? v) => v == null || v.trim().isEmpty ? null : v.trim();

final payrollRepositoryProvider =
    Provider<PayrollRepository>((ref) => PayrollRepository(ref.watch(dioProvider)));
