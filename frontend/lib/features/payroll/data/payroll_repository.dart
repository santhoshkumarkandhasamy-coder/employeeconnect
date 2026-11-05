import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/api/dio_client.dart';
import '../../attendance/domain/attendance_models.dart' show Paged;
import '../../export/data/export_repository.dart' show decodeErrorBody;
import '../domain/pay_items_models.dart';
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
}

final payrollRepositoryProvider =
    Provider<PayrollRepository>((ref) => PayrollRepository(ref.watch(dioProvider)));
