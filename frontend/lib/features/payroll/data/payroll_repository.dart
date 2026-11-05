import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/api/dio_client.dart';
import '../../attendance/domain/attendance_models.dart' show Paged;
import '../../export/data/export_repository.dart' show decodeErrorBody;
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
